"""Unit tests for the MCP-backed tool layer: guardrails, policy gotchas, saga compensation.

Design
------
**Zero network.** Every test replaces the ``mcp_call_sync`` reference *inside the
consuming modules* (``app.tools.hcm_tools`` / ``app.tools.itsm_tools``) with a
deterministic fake. Those modules do ``from app.tools.mcp_client import
mcp_call_sync``, which binds the function object at import time, so patching
``app.tools.mcp_client.mcp_call_sync`` alone would have no effect.

The fake reproduces the *verified* MCP envelope
(``{"status": "SUCCESS"|"REJECTED", "result": <str | decoded JSON>, "text": ...}``)
and reuses the production ``is_rejection`` lexer so the SUCCESS/REJECTED split is
derived exactly the way the real client derives it.

State isolation
---------------
``adapter.AUDIT_LOG_PATH`` and ``adapter.DB_PATH`` are redirected to ``tmp_path``
for every test, so the P2 idempotency store never leaks between tests (and the
repo's real ``artifacts/`` is never touched).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.tools import adapter, hcm_tools, itsm_tools
from app.tools.hcm_tools import (
    cancel_leave_request,
    get_current_employee_id,
    get_employee_profile,
    get_leave_balance,
    get_leave_requests,
    submit_leave_request,
    update_contact_info,
)
from app.tools.itsm_tools import (
    add_ticket_comment,
    create_incident,
    get_ticket,
    list_tickets,
    update_ticket_status,
)
from app.tools.mcp_client import is_rejection
from app.tools.policy_tools import (
    list_policy_concepts,
    read_policy_concept,
    search_policy,
)
from app.tools.saga_tools import execute_remote_work_transition_saga

EMP = "EMP-791"
OTHER = "EMP-1001"

# ══════════════════════════════════════════════════════════════════════════
# Verified fixtures — transcribed from the live-probe contract (mcp_contract.md)
# ══════════════════════════════════════════════════════════════════════════

BALANCES_REPORT = (
    "Employee EMP-791 Leave Balances:\n"
    "- Vacation: 15.0 days remaining (5.0/20.0 used)\n"
    "- Sick: 10.0 days remaining (0.0/10.0 used)"
)

PERSONAL_INFO_ADDRESS = "Singapore Office, 80 Pasir Panjang Rd, Singapore"
PERSONAL_INFO_PHONE = "+65-6521-0000"

LEAVE_REQUESTS: list[dict[str, Any]] = [
    {
        "request_id": 2966,
        "employee_id": EMP,
        "start_date": "2026-06-01",
        "end_date": "2026-06-05",
        "leave_type": "Vacation",
        "days": 5.0,
    }
]

TICKETS: list[dict[str, Any]] = [
    {
        "ticket_id": "INC0004555",
        "requested_by": EMP,
        "category": "Inquiry / Help",
        "short_description": "Onboarding setup and badges configuration",
        "status": "New",
        "priority": "3 - Moderate",
        "assignment_group": "Service Desk",
        "assigned_to": "ITIL User",
        "created_at": "2026-09-16T01:52:27.868908",
        "updated_at": "2026-09-16T01:52:27.868908",
        "updated_by": "admin",
        "caller_name": "Minsoojun Employee",
    }
]


def _envelope(server: str, tool: str, result: Any) -> dict[str, Any]:
    """Build the exact envelope `mcp_client.mcp_call_sync` returns."""
    text = result if isinstance(result, str) else None
    return {
        "status": "REJECTED" if is_rejection(text) else "SUCCESS",
        "result": result,
        "text": text,
        "server": server,
        "tool": tool,
    }


def _transport_error(server: str, tool: str) -> dict[str, Any]:
    """The payload `mcp_call_sync` returns when the transport blows up."""
    return {
        "status": "MCP_ERROR",
        "code": "MCP_TRANSPORT_ERROR",
        "server": server,
        "tool": tool,
        "message": "ExceptionGroup[HTTPStatusError: 429 Too Many Requests]",
    }


class FakeMcp:
    """Deterministic, offline stand-in for `mcp_call_sync`.

    Faithful to the verified contract: every tool answers with the same envelope
    shape the live servers produce, and business refusals are plain strings
    prefixed ``Error:`` / ``Denied:``.

    Writes are acknowledged but, by default, are **not** reflected back into the
    read views. That is deliberate: it lets a test replay the identical write
    twice and observe the P2 idempotency cache, instead of tripping the
    G-HCM-3 / G-ITSM-3 "you already have that" guardrails first.
    Personal info *is* mutated, because the saga's compensation step is only
    meaningful if the rollback is observable.
    """

    def __init__(
        self,
        *,
        tickets: list[dict[str, Any]] | None = None,
        leave_requests: list[dict[str, Any]] | None = None,
        balances_report: str = BALANCES_REPORT,
        address: str = PERSONAL_INFO_ADDRESS,
        phone: str = PERSONAL_INFO_PHONE,
        overrides: dict[str, Any] | None = None,
        fail_tools: set[str] | None = None,
        extra_balance_fields: dict[str, Any] | None = None,
        register_writes: bool = False,
    ) -> None:
        self.tickets = copy.deepcopy(TICKETS if tickets is None else tickets)
        self.leave_requests = copy.deepcopy(
            LEAVE_REQUESTS if leave_requests is None else leave_requests
        )
        self.balances_report = balances_report
        self.address = address
        self.phone = phone
        self.overrides = overrides or {}
        self.fail_tools = fail_tools or set()
        self.extra_balance_fields = extra_balance_fields or {}
        self.register_writes = register_writes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._next_ticket = 4600

    # -- introspection helpers ------------------------------------------------
    def tool_names(self) -> list[str]:
        return [c[1] for c in self.calls]

    def called(self, tool: str) -> bool:
        return tool in self.tool_names()

    def args_for(self, tool: str) -> list[dict[str, Any]]:
        return [c[2] for c in self.calls if c[1] == tool]

    # -- the callable itself --------------------------------------------------
    def __call__(
        self, server: str, tool_name: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = dict(args or {})
        self.calls.append((server, tool_name, args))

        if tool_name in self.fail_tools:
            return _transport_error(server, tool_name)

        if tool_name in self.overrides:
            override = self.overrides[tool_name]
            result = override(args) if callable(override) else override
            if isinstance(result, dict) and "status" in result:
                return result  # a raw envelope (e.g. an injected MCP_ERROR)
            envelope = _envelope(server, tool_name, result)
            envelope.update(
                self.extra_balance_fields
                if tool_name == "get_employee_balances"
                else {}
            )
            return envelope

        result = self._dispatch(tool_name, args)
        envelope = _envelope(server, tool_name, result)
        if tool_name == "get_employee_balances":
            envelope.update(self.extra_balance_fields)
        return envelope

    def _dispatch(self, tool: str, args: dict[str, Any]) -> Any:
        # ── WorkWeek ────────────────────────────────────────────────────────
        if tool == "get_current_employee_id":
            return EMP
        if tool == "get_employee_balances":
            return self.balances_report
        if tool == "get_personal_info":
            return (
                f"Employee {args.get('employee_id', EMP)} Personal Info:\n"
                f"- Address: {self.address}\n"
                f"- Phone: {self.phone}"
            )
        if tool == "update_personal_info":
            self.address = args.get("address", self.address)
            self.phone = args.get("phone", self.phone)
            return f"Success: Personal info updated for {args.get('employee_id', EMP)}."
        if tool == "get_leave_requests":
            return list(self.leave_requests)
        if tool == "request_time_off":
            if self.register_writes:
                self.leave_requests.append(
                    {
                        "request_id": 3000 + len(self.leave_requests),
                        "employee_id": args.get("employee_id", EMP),
                        "start_date": args.get("start_date"),
                        "end_date": args.get("end_date"),
                        "leave_type": args.get("leave_type"),
                        "days": args.get("days"),
                    }
                )
            return (
                f"Approved: Time off approved for {args.get('days')} days "
                f"({args.get('leave_type')}) from {args.get('start_date')} "
                f"to {args.get('end_date')}."
            )
        if tool == "cancel_leave_request":
            return (
                f"Success: Leave request {args.get('request_id')} cancelled and "
                "refunded successfully."
            )

        # ── ServiceImmediately ──────────────────────────────────────────────
        if tool == "list_tickets":
            return list(self.tickets)
        if tool == "create_ticket":
            self._next_ticket += 1
            ticket = {
                "ticket_id": f"INC000{self._next_ticket}",
                "requested_by": args.get("requested_by", EMP),
                "category": args.get("category"),
                "short_description": args.get("short_description"),
                "status": "New",
                "priority": args.get("priority"),
                "assignment_group": args.get("assignment_group", "Service Desk"),
                "assigned_to": "ITIL User",
                "created_at": "2026-09-16T02:00:00.000000",
                "updated_at": "2026-09-16T02:00:00.000000",
                "updated_by": "admin",
                "caller_name": "Minsoojun Employee",
            }
            if self.register_writes:
                self.tickets.append(ticket)
            return ticket
        if tool == "add_ticket_comment":
            return f"Success: Comment added to ticket {args.get('ticket_id')}."
        if tool == "update_ticket_status":
            return (
                f"Success: Ticket {args.get('ticket_id')} status updated to "
                f"{args.get('status')}."
            )

        raise AssertionError(f"FakeMcp has no stub for tool '{tool}'")


# ══════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Redirect the audit log and the P2 idempotency store into tmp_path.

    Without this, the idempotency cache in `artifacts/enterprise_state.db`
    survives across tests and silently turns real writes into replays.
    """
    monkeypatch.setattr(adapter, "AUDIT_LOG_PATH", tmp_path / "audit_logs.jsonl")
    monkeypatch.setattr(adapter, "DB_PATH", tmp_path / "enterprise_state.db")
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test reaches for the live MCP servers."""

    def _guard(server, tool_name, args=None):
        raise AssertionError(
            f"Unit tests must not hit the network (attempted {server}:{tool_name})."
        )

    monkeypatch.setattr(hcm_tools, "mcp_call_sync", _guard)
    monkeypatch.setattr(itsm_tools, "mcp_call_sync", _guard)


# The suite's fixed calendar. Every leave date below is expressed relative to
# this day, so the tests stay stable as real time moves on.
TODAY = "2026-09-16"


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """Pin `hcm_tools._today()` so date-sensitive guardrails are deterministic.

    The G-HCM-2 past-date check and the Section 18.2 notice-period check both
    read the wall clock. Without pinning, the hard-coded future dates in these
    tests would drift into the past and the expectations would invert.
    """
    monkeypatch.setenv(hcm_tools.TODAY_OVERRIDE_ENV, TODAY)


@pytest.fixture
def mcp_factory(monkeypatch):
    """Install a `FakeMcp` into both tool modules and hand it back."""

    def _install(**kwargs: Any) -> FakeMcp:
        fake = FakeMcp(**kwargs)
        monkeypatch.setattr(hcm_tools, "mcp_call_sync", fake)
        monkeypatch.setattr(itsm_tools, "mcp_call_sync", fake)
        return fake

    return _install


@pytest.fixture
def mcp(mcp_factory):
    return mcp_factory()


def audit_records(tmp_path) -> list[dict[str, Any]]:
    path = tmp_path / "audit_logs.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# ══════════════════════════════════════════════════════════════════════════
# Policy retrieval (local, no MCP)
# ══════════════════════════════════════════════════════════════════════════


def test_policy_search_returns_grounded_citations():
    """search_policy retrieves handbook sections with `Section X.Y` citations."""
    res = search_policy("host gift card $50 limit")
    assert res["status"] == "SUCCESS"
    assert len(res["results"]) > 0
    combined = " ".join(r["content"].lower() for r in res["results"])
    assert "gift" in combined
    assert all(r.get("citation_url") for r in res["results"])

    concepts = list_policy_concepts()
    assert concepts["status"] == "SUCCESS"
    assert concepts["total_sections"] > 10

    assert read_policy_concept("25")["status"] == "SUCCESS"


# ══════════════════════════════════════════════════════════════════════════
# P6 — RBAC data isolation
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "call",
    [
        lambda: get_employee_profile(employee_id=EMP, caller_id=OTHER),
        lambda: get_leave_balance(employee_id=EMP, caller_id=OTHER),
        lambda: get_leave_requests(employee_id=EMP, caller_id=OTHER),
        lambda: list_tickets(employee_id=EMP, caller_id=OTHER),
    ],
)
def test_p6_rbac_blocks_cross_employee_reads(mcp, call):
    """P6: a caller may never read another employee's records."""
    res = call()
    assert res["status"] == "DENIED"
    assert res["code"] == "RBAC_VIOLATION"
    assert mcp.calls == [], "the MCP server must never be contacted on an RBAC denial"


@pytest.mark.parametrize(
    "call",
    [
        lambda: update_contact_info(
            employee_id=EMP, phone="+65-9123-4567", caller_id=OTHER, user_confirmed=True
        ),
        lambda: submit_leave_request(
            employee_id=EMP,
            leave_type="vacation",
            start_date="2026-12-01",
            end_date="2026-12-02",
            days=2.0,
            caller_id=OTHER,
            user_confirmed=True,
        ),
        lambda: cancel_leave_request(
            employee_id=EMP, request_id=2966, caller_id=OTHER, user_confirmed=True
        ),
        lambda: create_incident(
            employee_id=EMP,
            category="Hardware",
            short_description="New mouse",
            priority=3,
            caller_id=OTHER,
            user_confirmed=True,
        ),
        lambda: execute_remote_work_transition_saga(
            employee_id=EMP, caller_id=OTHER, user_confirmed=True
        ),
    ],
)
def test_p6_rbac_blocks_cross_employee_writes(mcp, call):
    """P6: a caller may never write to another employee's records."""
    res = call()
    assert res["status"] == "DENIED"
    assert res["code"] == "RBAC_VIOLATION"
    assert mcp.calls == []


# ══════════════════════════════════════════════════════════════════════════
# P3 — HITL confirmation gate
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: update_contact_info(
                employee_id=EMP, phone="+65-9123-4567", caller_id=EMP
            ),
            id="update_contact_info",
        ),
        pytest.param(
            lambda: submit_leave_request(
                employee_id=EMP,
                leave_type="vacation",
                start_date="2026-12-01",
                end_date="2026-12-02",
                days=2.0,
                caller_id=EMP,
            ),
            id="submit_leave_request",
        ),
        pytest.param(
            lambda: cancel_leave_request(
                employee_id=EMP, request_id=2966, caller_id=EMP
            ),
            id="cancel_leave_request",
        ),
        pytest.param(
            lambda: create_incident(
                employee_id=EMP,
                category="Hardware",
                short_description="Docking station replacement",
                priority=3,
                caller_id=EMP,
            ),
            id="create_incident",
        ),
        pytest.param(
            lambda: update_ticket_status(
                ticket_id="INC0004555", new_status="In Progress", caller_id=EMP
            ),
            id="update_ticket_status",
        ),
        pytest.param(
            lambda: execute_remote_work_transition_saga(employee_id=EMP, caller_id=EMP),
            id="saga",
        ),
    ],
)
def test_p3_every_write_tool_requires_confirmation(mcp, call):
    """P3: no write reaches the backend without explicit human confirmation."""
    res = call()
    assert res["status"] == "CONFIRMATION_REQUIRED"
    for mutating in (
        "update_personal_info",
        "request_time_off",
        "cancel_leave_request",
        "create_ticket",
        "update_ticket_status",
    ):
        assert not mcp.called(mutating), f"{mutating} was called before confirmation"


# ══════════════════════════════════════════════════════════════════════════
# P2 — idempotency replay
# ══════════════════════════════════════════════════════════════════════════


def test_p2_identical_leave_request_is_replayed_not_rebooked(mcp):
    """P2: replaying the same confirmed write returns the cached response."""
    kwargs = {
        "employee_id": EMP,
        "leave_type": "vacation",
        "start_date": "2026-12-01",
        "end_date": "2026-12-02",
        "days": 2.0,
        "caller_id": EMP,
        "user_confirmed": True,
        "toil_cleared_by_manager": True,
    }
    first = submit_leave_request(**kwargs)
    assert first["status"] == "SUCCESS"
    assert first.get("idempotent_replay") is None

    second = submit_leave_request(**kwargs)
    assert second["status"] == "SUCCESS"
    assert second["idempotent_replay"] is True
    assert second["idempotency_key"] == first["idempotency_key"]
    assert mcp.tool_names().count("request_time_off") == 1


def test_p2_identical_incident_is_replayed_not_recreated(mcp):
    """P2: create_incident is protected by the same replay cache."""
    kwargs = {
        "employee_id": EMP,
        "category": "Hardware",
        "short_description": "Laptop docking station replacement",
        "priority": 3,
        "caller_id": EMP,
        "user_confirmed": True,
    }
    first = create_incident(**kwargs)
    assert first["status"] == "SUCCESS"

    second = create_incident(**kwargs)
    assert second["idempotent_replay"] is True
    assert second["ticket_id"] == first["ticket_id"]
    assert mcp.tool_names().count("create_ticket") == 1


# ══════════════════════════════════════════════════════════════════════════
# G-HCM-1 / 2 / 3 / 4 / 5 — leave guardrails
# ══════════════════════════════════════════════════════════════════════════


def test_g_hcm_1_insufficient_balance(mcp):
    """G-HCM-1: requested days may not exceed the live WorkWeek balance."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-12-01",
        end_date="2026-12-31",
        days=99.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-1_INSUFFICIENT_BALANCE"
    assert res["available_balance"] == 15.0
    assert not mcp.called("request_time_off")


def test_g_hcm_2_end_before_start(mcp):
    """G-HCM-2: end_date may not precede start_date."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-11-15",
        end_date="2026-11-10",
        days=3.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-2_INVALID_DATE_RANGE"
    assert mcp.calls == [], "a malformed range must be rejected before any MCP read"


def test_g_hcm_2_non_positive_days(mcp):
    """G-HCM-2: `days` must be strictly positive."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-11-10",
        end_date="2026-11-10",
        days=0.0,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-2_INVALID_DATE_RANGE"


def test_g_hcm_2_invalid_date_format(mcp):
    """G-HCM-2: non ISO-8601 dates are rejected locally."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="01/12/2026",
        end_date="02/12/2026",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-2_INVALID_DATE_FORMAT"


def test_g_hcm_2_section_18_2_notice_period(mcp):
    """Section 18.2: unpaid personal leave needs 30 days notice (reference 2026-09-16)."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="personal",
        start_date="2026-09-25",
        end_date="2026-09-26",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        is_emergency=False,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-2_NOTICE_PERIOD_VIOLATION"
    assert "18.2" in res["citation"]


# The default fixture seeds the *real* WorkWeek record (2026-06-01..05), which
# sits in the past relative to TODAY and is therefore short-circuited by the
# G-HCM-2 past-date guardrail. Overlap tests need a future window instead.
FUTURE_LEAVE_REQUESTS: list[dict[str, Any]] = [
    {
        "request_id": 2966,
        "employee_id": EMP,
        "start_date": "2026-12-14",
        "end_date": "2026-12-18",
        "leave_type": "Vacation",
        "days": 5.0,
    }
]


def test_g_hcm_2_start_date_in_the_past_is_rejected(mcp):
    """G-HCM-2: leave cannot be booked retroactively.

    WorkWeek does not enforce this — its leave-type check fires first and masks
    every later validation — so this layer is the only thing stopping a typo
    from creating a back-dated record.
    """
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2020-01-01",
        end_date="2020-01-02",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-2_START_DATE_IN_PAST"
    assert not mcp.called("request_time_off")


def test_g_hcm_2_today_is_not_in_the_past(mcp):
    """Boundary: booking leave that starts today must still be allowed."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date=TODAY,
        end_date=TODAY,
        days=1.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "SUCCESS"
    assert mcp.called("request_time_off")


def test_g_hcm_3_overlap_is_the_only_line_of_defence(mcp_factory):
    """G-HCM-3: the backend cannot detect overlapping leave — this layer must.

    The seeded history holds 2026-12-14..2026-12-18; a 12-15..12-16 request is
    fully contained inside it.
    """
    mcp = mcp_factory(leave_requests=FUTURE_LEAVE_REQUESTS)
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-12-15",
        end_date="2026-12-16",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-3_OVERLAPPING_LEAVE"
    assert res["conflicting_request"]["request_id"] == 2966
    assert not mcp.called("request_time_off")


def test_g_hcm_3_adjacent_dates_do_not_overlap(mcp_factory):
    """G-HCM-3 must not over-block: the day after an existing block is fine."""
    mcp = mcp_factory(leave_requests=FUTURE_LEAVE_REQUESTS)
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-12-19",
        end_date="2026-12-20",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "SUCCESS"
    assert mcp.called("request_time_off")


def test_g_hcm_4_toil_must_be_consumed_before_vacation(mcp):
    """Section 25.3 / G-HCM-4: accrued TOIL blocks a Paid Vacation request.

    WorkWeek does not track TOIL (Section 25.3 keeps it offline with the Line
    Manager, and `get_employee_balances` only reports Vacation and Sick), so
    the balance is supplied by the caller via `accrued_toil_days`.
    """
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-11-02",
        end_date="2026-11-04",
        days=3.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=False,
        accrued_toil_days=2.0,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-4_TOIL_MUST_BE_USED_FIRST"
    assert "25.3" in res["citation"]
    assert not mcp.called("request_time_off")

    cleared = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-11-02",
        end_date="2026-11-04",
        days=3.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
        accrued_toil_days=2.0,
    )
    assert cleared["status"] == "SUCCESS"


def test_g_hcm_4_no_toil_does_not_block_vacation(mcp):
    """With no accrued TOIL the guardrail must stay out of the way."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-11-09",
        end_date="2026-11-10",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=False,
    )
    assert res["status"] == "SUCCESS"
    assert mcp.called("request_time_off")


def test_g_hcm_4_falls_back_to_a_toil_line_in_the_balance_report(mcp_factory):
    """If WorkWeek ever reports TOIL, the guardrail picks it up without a code change.

    `_extract_balances` keys on whatever leave types the report lists, so a
    "- TOIL: 1.5 days remaining" line lands as `balances["toil"]`.
    """
    mcp = mcp_factory(
        balances_report=(
            f"Employee {EMP} Leave Balances:\n"
            "- Vacation: 15.0 days remaining (5.0/20.0 used)\n"
            "- Sick: 10.0 days remaining (0.0/10.0 used)\n"
            "- TOIL: 1.5 days remaining"
        )
    )
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-11-16",
        end_date="2026-11-17",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=False,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-4_TOIL_MUST_BE_USED_FIRST"
    assert not mcp.called("request_time_off")


def test_g_hcm_5_sick_leave_over_two_days_needs_mc(mcp):
    """Section 19.2 / G-HCM-5: >2 consecutive sick days requires a medical certificate."""
    denied = submit_leave_request(
        employee_id=EMP,
        leave_type="sick",
        start_date="2026-10-12",
        end_date="2026-10-15",
        days=4.0,
        caller_id=EMP,
        user_confirmed=True,
        has_medical_certificate=False,
    )
    assert denied["status"] == "DENIED"
    assert denied["code"] == "G-HCM-5_MEDICAL_CERTIFICATE_REQUIRED"
    assert "19.2" in denied["citation"]
    assert not mcp.called("request_time_off")

    allowed = submit_leave_request(
        employee_id=EMP,
        leave_type="sick",
        start_date="2026-10-12",
        end_date="2026-10-15",
        days=4.0,
        caller_id=EMP,
        user_confirmed=True,
        has_medical_certificate=True,
    )
    assert allowed["status"] == "SUCCESS"
    assert mcp.args_for("request_time_off")[0]["leave_type"] == "Sick"


def test_g_hcm_5_two_sick_days_need_no_mc(mcp):
    """The §19.2 threshold is *more than* two days, not two."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="sick",
        start_date="2026-10-12",
        end_date="2026-10-13",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        has_medical_certificate=False,
    )
    assert res["status"] == "SUCCESS"


def test_section_25_3_toil_is_never_recorded_in_workweek(mcp):
    """Section 25.3: `leave_type='toil'` is refused with manager-coordination guidance."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="toil",
        start_date="2026-11-02",
        end_date="2026-11-03",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "POLICY_RULE_SECTION_25_3_TOIL_NOT_IN_WORKWEEK"
    assert mcp.calls == []


@pytest.mark.parametrize(
    ("leave_type", "citation_fragment"),
    [("childcare", "24.2"), ("personal", "18.2")],
)
def test_childcare_and_personal_leave_are_not_bookable_in_workweek(
    mcp, leave_type, citation_fragment
):
    """Neither type exists on the WorkWeek backend; they route to HR / the line manager."""
    res = submit_leave_request(
        employee_id=EMP,
        leave_type=leave_type,
        start_date="2027-03-01",  # far future, so §18.2 notice is satisfied
        end_date="2027-03-02",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "LEAVE_TYPE_NOT_BOOKABLE_IN_WORKWEEK"
    assert citation_fragment in res["citation"]
    assert not mcp.called("request_time_off")


def test_unknown_leave_type_is_rejected(mcp):
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="sabbatical",
        start_date="2026-12-01",
        end_date="2026-12-02",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "INVALID_LEAVE_TYPE"
    assert mcp.calls == []


# ══════════════════════════════════════════════════════════════════════════
# G-HCM-6 / 7 — contact-info field validation
# ══════════════════════════════════════════════════════════════════════════


def test_g_hcm_6_address_shorter_than_five_characters(mcp):
    res = update_contact_info(
        employee_id=EMP, address="abc", caller_id=EMP, user_confirmed=True
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-6_INVALID_ADDRESS"
    assert not mcp.called("update_personal_info")


@pytest.mark.parametrize("phone", ["nope!!", "123", "+65 9123 4567 ext.12345678901234"])
def test_g_hcm_7_invalid_phone_format(mcp, phone):
    res = update_contact_info(
        employee_id=EMP, phone=phone, caller_id=EMP, user_confirmed=True
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-7_INVALID_PHONE"
    assert not mcp.called("update_personal_info")


def test_update_contact_info_backfills_the_untouched_field(mcp):
    """WorkWeek's update_personal_info demands both fields; the unchanged one is backfilled."""
    res = update_contact_info(
        employee_id=EMP, phone="+65-9123-4567", caller_id=EMP, user_confirmed=True
    )
    assert res["status"] == "SUCCESS"
    assert res["updated_fields"] == {"phone": "+65-9123-4567"}
    sent = mcp.args_for("update_personal_info")[0]
    assert sent["address"] == PERSONAL_INFO_ADDRESS
    assert sent["phone"] == "+65-9123-4567"


# ══════════════════════════════════════════════════════════════════════════
# G-ITSM-1 / 2 / 3 — ticket guardrails
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("target", ["Closed", "Resolved"])
def test_g_itsm_1_new_cannot_jump_to_closed_or_resolved(mcp, target):
    """§5.5 / G-ITSM-1: the lifecycle must be sequential — stricter than the backend."""
    res = update_ticket_status(
        ticket_id="INC0004555", new_status=target, caller_id=EMP, user_confirmed=True
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-ITSM-1_INVALID_STATE_TRANSITION"
    assert res["current_status"] == "New"
    assert not mcp.called("update_ticket_status")


def test_g_itsm_1_allows_new_to_in_progress(mcp):
    res = update_ticket_status(
        ticket_id="INC0004555",
        new_status="In Progress",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "SUCCESS"
    assert res["previous_status"] == "New"
    sent = mcp.args_for("update_ticket_status")[0]
    # The backend validates `updated_by` against the caller identity.
    assert sent["updated_by"] == EMP


def test_g_itsm_1_closed_ticket_is_terminal(mcp_factory):
    mcp = mcp_factory(
        tickets=[{**TICKETS[0], "ticket_id": "INC0004596", "status": "Closed"}]
    )
    res = update_ticket_status(
        ticket_id="INC0004596",
        new_status="In Progress",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-ITSM-1_INVALID_STATE_TRANSITION"
    assert res["allowed_transitions"] == []
    assert not mcp.called("update_ticket_status")


@pytest.mark.parametrize("priority", [0, 9, "urgent-ish"])
def test_g_itsm_2_priority_out_of_range(mcp, priority):
    res = create_incident(
        employee_id=EMP,
        category="IT_Support",
        short_description="Bad priority test",
        priority=priority,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-ITSM-2_INVALID_PRIORITY"
    assert not mcp.called("create_ticket")


def test_g_itsm_2_critical_requires_an_outage(mcp):
    res = create_incident(
        employee_id=EMP,
        category="Hardware",
        short_description="Need a new mouse",
        priority=1,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-ITSM-2_CRITICAL_REQUIRES_OUTAGE"
    assert not mcp.called("create_ticket")


def test_g_itsm_2_critical_accepted_with_outage_language(mcp):
    res = create_incident(
        employee_id=EMP,
        category="IT_Support",
        short_description="Production payroll system outage — nobody can log in",
        priority=1,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "SUCCESS"
    assert mcp.args_for("create_ticket")[0]["priority"] == "1 - Critical"


def test_invalid_category_is_rejected(mcp):
    """The backend does not validate `category`; this layer must."""
    res = create_incident(
        employee_id=EMP,
        category="Nonsense Category",
        short_description="Anything",
        priority=3,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "INVALID_CATEGORY"
    assert not mcp.called("create_ticket")


def test_g_itsm_3_duplicate_open_ticket(mcp_factory):
    mcp = mcp_factory(
        tickets=[
            {
                **TICKETS[0],
                "ticket_id": "INC0004701",
                "category": "IT_Support",
                "short_description": "VPN certificate renewal issue",
                "status": "In Progress",
            }
        ]
    )
    res = create_incident(
        employee_id=EMP,
        category="IT_Support",
        short_description="VPN Certificate Renewal Issue",
        priority=3,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-ITSM-3_DUPLICATE_INCIDENT"
    assert res["existing_ticket_id"] == "INC0004701"
    assert not mcp.called("create_ticket")


def test_g_itsm_3_ignores_closed_tickets(mcp_factory):
    """A closed lookalike must not block a fresh report of the same problem."""
    mcp = mcp_factory(
        tickets=[
            {
                **TICKETS[0],
                "ticket_id": "INC0004702",
                "category": "IT_Support",
                "short_description": "VPN certificate renewal issue",
                "status": "Closed",
            }
        ]
    )
    res = create_incident(
        employee_id=EMP,
        category="IT_Support",
        short_description="VPN certificate renewal issue",
        priority=3,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "SUCCESS"
    assert mcp.called("create_ticket")


def test_p6_ticket_ownership_is_enforced_by_visibility(mcp):
    """A ticket absent from the caller's own list is simply not reachable (P6)."""
    res = get_ticket("INC9999999", caller_id=EMP)
    assert res["status"] == "ERROR"
    assert res["code"] == "TICKET_NOT_FOUND"

    comment = add_ticket_comment("INC9999999", "hello", caller_id=EMP)
    assert comment["code"] == "TICKET_NOT_FOUND"
    assert not mcp.called("add_ticket_comment")


# ══════════════════════════════════════════════════════════════════════════
# Section 5.4 — Home Office Equipment Allowance
# ══════════════════════════════════════════════════════════════════════════


def test_section_5_4_exceeds_500_usd_cap(mcp):
    res = create_incident(
        employee_id=EMP,
        category="Facilities",
        short_description="Home office equipment allowance: standing desk",
        priority=3,
        amount_usd=900.0,
        is_home_office_equipment=True,
        work_arrangement="Remote",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "POLICY_RULE_SECTION_5_4_EXCEEDS_500_USD_CAP"
    assert "5.4" in res["citation"]
    assert not mcp.called("create_ticket")


def test_section_5_4_requires_facilities_category(mcp):
    res = create_incident(
        employee_id=EMP,
        category="IT_Support",
        short_description="Home office equipment allowance: monitor",
        priority=3,
        amount_usd=350.0,
        is_home_office_equipment=True,
        work_arrangement="Remote",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "POLICY_RULE_SECTION_5_4_WRONG_CATEGORY_MUST_BE_FACILITIES"
    assert not mcp.called("create_ticket")


def test_section_5_4_requires_remote_or_hybrid(mcp):
    res = create_incident(
        employee_id=EMP,
        category="Facilities",
        short_description="Home office equipment allowance: ergonomic chair",
        priority=3,
        amount_usd=300.0,
        is_home_office_equipment=True,
        work_arrangement="On-site",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "POLICY_RULE_SECTION_5_4_INELIGIBLE_WORK_ARRANGEMENT"
    assert not mcp.called("create_ticket")


def test_section_5_4_compliant_request_succeeds(mcp):
    res = create_incident(
        employee_id=EMP,
        category="Facilities",
        short_description="Home office equipment allowance: ergonomic chair",
        priority=3,
        amount_usd=450.0,
        is_home_office_equipment=True,
        work_arrangement="Hybrid",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "SUCCESS"
    assert res["ticket_id"].startswith("INC")


# ══════════════════════════════════════════════════════════════════════════
# P5 — graceful degradation / fail closed
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("failing_tool", "call", "system"),
    [
        (
            "get_employee_balances",
            lambda: get_leave_balance(EMP, EMP),
            "WorkWeek",
        ),
        (
            "get_personal_info",
            lambda: get_employee_profile(EMP, EMP),
            "WorkWeek",
        ),
        (
            "list_tickets",
            lambda: list_tickets(EMP, EMP),
            "ServiceImmediately",
        ),
    ],
)
def test_p5_reads_degrade_to_service_unavailable(
    mcp_factory, failing_tool, call, system
):
    mcp_factory(fail_tools={failing_tool})
    res = call()
    assert res["status"] == "SERVICE_UNAVAILABLE"
    assert res["code"] == "MCP_TRANSPORT_ERROR"
    assert res["system"] == system
    assert res["message"]  # user-facing guidance, not a stack trace


def test_p5_writes_fail_closed_when_mcp_is_down(mcp_factory):
    """A write must never be attempted when the pre-flight read failed."""
    mcp = mcp_factory(fail_tools={"get_employee_balances"})
    res = submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-12-01",
        end_date="2026-12-02",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "SERVICE_UNAVAILABLE"
    assert not mcp.called("request_time_off")


# ══════════════════════════════════════════════════════════════════════════
# Backend refusal (REJECTED) → DENIED
# ══════════════════════════════════════════════════════════════════════════


def test_workweek_rejection_is_surfaced_as_denied(mcp_factory):
    """`Error:` / `Denied:` prefixed replies are refusals, not faults."""
    mcp = mcp_factory(
        overrides={"update_personal_info": "Error: Invalid phone number format."}
    )
    res = update_contact_info(
        employee_id=EMP, phone="+65-9123-4567", caller_id=EMP, user_confirmed=True
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "WORKWEEK_UPDATE_REJECTED"
    assert res["message"] == "Error: Invalid phone number format."
    assert mcp.called("update_personal_info")


def test_service_immediately_rejection_is_surfaced_as_denied(mcp_factory):
    mcp_factory(
        tickets=[{**TICKETS[0], "status": "In Progress"}],
        overrides={
            "update_ticket_status": "Denied: Invalid status transition from New to Resolved."
        },
    )
    res = update_ticket_status(
        ticket_id="INC0004555",
        new_status="Resolved",
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "ITSM_TRANSITION_REJECTED"
    assert "Invalid status transition" in res["message"]


def test_unknown_employee_rejection(mcp_factory):
    mcp_factory(overrides={"get_personal_info": "Employee EMP-791 not found."})
    res = get_employee_profile(EMP, EMP)
    assert res["status"] == "DENIED"
    assert res["code"] == "WORKWEEK_EMPLOYEE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════════════════
# P7 — audit logging
# ══════════════════════════════════════════════════════════════════════════


def test_p7_audit_log_records_both_allow_and_deny(mcp, isolated_state):
    get_leave_balance(EMP, EMP)  # ALLOW
    get_leave_balance(employee_id=EMP, caller_id=OTHER)  # DENY (P6)
    submit_leave_request(
        employee_id=EMP,
        leave_type="sick",
        start_date="2026-10-12",
        end_date="2026-10-15",
        days=4.0,
        caller_id=EMP,
        user_confirmed=True,
    )  # DENY (G-HCM-5)

    records = audit_records(isolated_state)
    assert len(records) >= 3

    allows = [r for r in records if r["decision"] == "ALLOW"]
    denies = [r for r in records if r["decision"] == "DENY"]
    assert allows and denies

    reasons = {r["deny_reason"] for r in denies}
    assert any("RBAC_VIOLATION" in str(r) for r in reasons)
    assert "G-HCM-5_MEDICAL_CERTIFICATE_REQUIRED" in reasons

    # The 18-field envelope must stay intact.
    for field in (
        "event_id",
        "timestamp",
        "actor",
        "target_employee_id",
        "tool_name",
        "target_system",
        "action_type",
        "decision",
        "downstream_request_id",
        "region",
        "pii_masked",
        "request_summary",
    ):
        assert field in records[0]


def test_p7_confirmed_write_is_attributed_to_the_human(mcp, isolated_state):
    submit_leave_request(
        employee_id=EMP,
        leave_type="vacation",
        start_date="2026-12-01",
        end_date="2026-12-02",
        days=2.0,
        caller_id=EMP,
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    writes = [
        r
        for r in audit_records(isolated_state)
        if r["tool_name"] == "submit_leave_request" and r["action_type"] == "WRITE"
    ]
    assert writes
    assert writes[-1]["actor"]["origin"] == "HUMAN_CONFIRMED"
    assert writes[-1]["idempotency_key"]


# ══════════════════════════════════════════════════════════════════════════
# Saga — cross-system orchestration and compensation
# ══════════════════════════════════════════════════════════════════════════


def test_saga_rolls_back_workweek_when_step_2_fails(mcp):
    """SDD §3.5 / §5.6: a Step-2 failure must compensate Step 1 back to the snapshot."""
    res = execute_remote_work_transition_saga(
        employee_id=EMP,
        new_address="999 Failed Saga Street, Singapore",
        new_work_arrangement="Remote",
        equipment_amount_usd=400.0,
        caller_id=EMP,
        user_confirmed=True,
        simulate_itsm_failure=True,
    )
    assert res["status"] == "SAGA_COMPENSATED_ROLLED_BACK"
    assert res["compensation_action"]["status"] == "ROLLED_BACK_SUCCESSFULLY"
    assert res["compensation_action"]["reverted_address"] == PERSONAL_INFO_ADDRESS
    assert res["failure_code"] == "SIMULATED_ITSM_OUTAGE"

    # Step 1 wrote the new address, compensation wrote the snapshot back.
    writes = mcp.args_for("update_personal_info")
    assert len(writes) == 2
    assert writes[0]["address"] == "999 Failed Saga Street, Singapore"
    assert writes[1]["address"] == PERSONAL_INFO_ADDRESS
    assert writes[1]["phone"] == PERSONAL_INFO_PHONE

    # And the simulated outage means no ticket was ever created.
    assert not mcp.called("create_ticket")

    # Observable end state on the fake backend is the pre-saga snapshot.
    assert mcp.address == PERSONAL_INFO_ADDRESS
    assert mcp.phone == PERSONAL_INFO_PHONE


def test_saga_completes_when_both_steps_succeed(mcp):
    res = execute_remote_work_transition_saga(
        employee_id=EMP,
        new_address="77 Success Remote Ave, Singapore 123456",
        new_work_arrangement="Remote",
        equipment_title="Home Office Ergonomic Desk",
        equipment_amount_usd=420.0,
        caller_id=EMP,
        user_confirmed=True,
        simulate_itsm_failure=False,
    )
    assert res["status"] == "SAGA_COMPLETED"
    assert (
        res["step_1_workweek"]["updated_address"]
        == "77 Success Remote Ave, Singapore 123456"
    )
    assert res["step_2_service_immediately"]["ticket_id"].startswith("INC")
    assert mcp.address == "77 Success Remote Ave, Singapore 123456"
    assert mcp.args_for("create_ticket")[0]["category"] == "Facilities"


def test_saga_rolls_back_on_a_real_section_5_4_violation(mcp):
    """The compensation path is driven by a genuine guardrail denial, not only the simulator."""
    res = execute_remote_work_transition_saga(
        employee_id=EMP,
        new_address="12 Over Budget Road, Singapore",
        new_work_arrangement="Remote",
        equipment_amount_usd=5000.0,
        caller_id=EMP,
        user_confirmed=True,
    )
    assert res["status"] == "SAGA_COMPENSATED_ROLLED_BACK"
    assert res["failure_code"] == "POLICY_RULE_SECTION_5_4_EXCEEDS_500_USD_CAP"
    assert mcp.address == PERSONAL_INFO_ADDRESS
    assert not mcp.called("create_ticket")


# ══════════════════════════════════════════════════════════════════════════
# Happy-path read plumbing (envelope parsing)
# ══════════════════════════════════════════════════════════════════════════


def test_reads_parse_the_verified_envelope_shapes(mcp):
    """Human-readable reports are regex-parsed; JSON-in-string payloads are decoded."""
    assert get_current_employee_id(caller_id=EMP)["employee_id"] == EMP

    profile = get_employee_profile(EMP, EMP)
    assert profile["status"] == "SUCCESS"
    assert profile["profile"]["address"] == PERSONAL_INFO_ADDRESS
    assert profile["profile"]["phone"] == PERSONAL_INFO_PHONE

    balance = get_leave_balance(EMP, EMP)
    assert balance["status"] == "SUCCESS"
    assert balance["balances"]["vacation"] == 15.0
    assert balance["balances"]["sick"] == 10.0
    assert balance["balances"]["vacation_used"] == 5.0
    assert balance["balances"]["vacation_entitlement"] == 20.0
    assert len(balance["existing_requests"]) == 1

    requests = get_leave_requests(EMP, EMP)
    assert requests["status"] == "SUCCESS"
    assert requests["requests"][0]["request_id"] == 2966

    tickets = list_tickets(EMP, EMP)
    assert tickets["status"] == "SUCCESS"
    assert tickets["count"] == 1
    assert tickets["tickets"][0]["ticket_id"] == "INC0004555"


def test_cancel_leave_request_round_trip(mcp):
    missing = cancel_leave_request(employee_id=EMP, caller_id=EMP, user_confirmed=True)
    assert missing["status"] == "ERROR"
    assert missing["code"] == "MISSING_REQUEST_ID"

    res = cancel_leave_request(
        employee_id=EMP, request_id=2966, caller_id=EMP, user_confirmed=True
    )
    assert res["status"] == "SUCCESS"
    assert mcp.args_for("cancel_leave_request")[0]["request_id"] == 2966


def test_add_ticket_comment_on_an_owned_ticket(mcp):
    res = add_ticket_comment("INC0004555", "Following up on this.", caller_id=EMP)
    assert res["status"] == "SUCCESS"
    sent = mcp.args_for("add_ticket_comment")[0]
    assert sent["author"] == EMP
    assert sent["comment"] == "Following up on this."
