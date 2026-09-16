"""Live, read-only integration probe against the real MCP servers.

Skipped automatically when no `MCP_TOKEN` is configured, so the suite stays
green on a machine without credentials.

> [!IMPORTANT]
> **Read-only by design.** The mock host rate-limits aggressively (HTTP 429)
> and is shared with the demo UI, so this module must never mutate live state:
> no `request_time_off`, no `create_ticket`, no status transitions. Guardrail
> behaviour is covered exhaustively (and offline) in
> `tests/unit/test_guardrails_and_tools.py`; full write coverage lives in the
> manual `tests/mcp_smoke.py` script.
"""

from __future__ import annotations

import pytest

from app.tools.hcm_tools import get_current_employee_id, get_leave_balance
from app.tools.itsm_tools import list_tickets
from app.tools.mcp_client import mcp_enabled

pytestmark = pytest.mark.skipif(
    not mcp_enabled(),
    reason="MCP_TOKEN is not configured; skipping live MCP integration tests.",
)

EMP = "EMP-791"


@pytest.fixture(scope="module")
def employee_id() -> str:
    """Resolve the authenticated employee once and reuse it (one call, not three)."""
    res = get_current_employee_id(caller_id=EMP)
    if res.get("status") == "SERVICE_UNAVAILABLE":
        pytest.skip(f"WorkWeek MCP unreachable: {res.get('detail')}")
    assert res["status"] == "SUCCESS"
    return str(res["employee_id"]).strip()


def test_get_current_employee_id_resolves_the_token_identity(employee_id):
    """The Personal Access Token maps to exactly one employee context."""
    assert employee_id == EMP


def test_get_leave_balance_returns_numeric_balances(employee_id):
    res = get_leave_balance(employee_id, employee_id)
    if res.get("status") == "SERVICE_UNAVAILABLE":
        pytest.skip(f"WorkWeek MCP unreachable: {res.get('detail')}")

    assert res["status"] == "SUCCESS"
    balances = res["balances"]
    assert {"vacation", "sick"} <= balances.keys()
    for key in ("vacation", "sick"):
        assert isinstance(balances[key], float)
        assert balances[key] >= 0.0
    assert "Leave Balances" in res["balances_report"]
    assert isinstance(res["existing_requests"], list)


def test_list_tickets_returns_a_list(employee_id):
    res = list_tickets(employee_id, employee_id)
    if res.get("status") == "SERVICE_UNAVAILABLE":
        pytest.skip(f"ServiceImmediately MCP unreachable: {res.get('detail')}")

    assert res["status"] == "SUCCESS"
    assert isinstance(res["tickets"], list)
    assert res["count"] == len(res["tickets"])
    for ticket in res["tickets"]:
        assert ticket.get("ticket_id")
        assert ticket.get("status")


def test_rbac_denial_never_reaches_the_network():
    """P6 is enforced locally, so this costs no request against the rate limit."""
    res = list_tickets(employee_id="EMP-1001", caller_id=EMP)
    assert res["status"] == "DENIED"
    assert res["code"] == "RBAC_VIOLATION"
