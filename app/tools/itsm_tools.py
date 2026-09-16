"""ServiceImmediately ITSM Tools — MCP-backed (SDD §5.1, §5.3, UC-1.3).

System of record: the **ServiceImmediately MCP server** mounted at
`/service-immediately/mcp/` on the Unified Mock Enterprise Services host.

The local layer remains the Policy Enforcement Point (Principle P1/P2): the SDD
business guardrails are evaluated **before** the MCP call, so a rejected request
never reaches the downstream SaaS.

Note on the state machine: ServiceImmediately itself permits `New -> Closed`, but
SDD guardrail **G-ITSM-1** deliberately forbids that shortcut so every incident
carries a triage and resolution trail. Our policy layer is intentionally stricter
than the platform.
"""

from __future__ import annotations

from typing import Any

from app.tools.adapter import (
    check_idempotency,
    compute_idempotency_key,
    enforce_data_isolation,
    log_audit_event,
    record_idempotency,
)
from app.tools.mcp_client import mcp_call_sync

SERVER = "serviceimmediately"

# SDD G-ITSM-1 state machine (stricter than the ServiceImmediately backend).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "New": {"In Progress", "Cancelled"},
    "In Progress": {"Resolved", "Closed", "Cancelled"},
    "Resolved": {"Closed", "In Progress"},
    "Closed": set(),
    "Cancelled": set(),
}

# ServiceImmediately priority vocabulary.
PRIORITY_LABELS = {
    1: "1 - Critical",
    2: "2 - High",
    3: "3 - Moderate",
    4: "4 - Low",
}
CRITICAL_KEYWORDS = (
    "outage",
    "crash",
    "down",
    "downtime",
    "unavailable",
    "offline",
    "cannot access",
    "not working",
    "data loss",
    "breach",
)

VALID_CATEGORIES = (
    "IT_Support",
    "Hardware",
    "Software",
    "Facilities",
    "Security",
    "HR",
    # Observed on the live ServiceImmediately instance.
    "Inquiry / Help",
)


def _result_of(payload: Any) -> Any:
    """Unwrap the `result` field of an `mcp_call_sync` envelope."""
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _mcp_failed(payload: Any) -> bool:
    """True only for transport/config faults — NOT for business rejections."""
    return isinstance(payload, dict) and payload.get("status") in (
        "MCP_ERROR",
        "MCP_NOT_CONFIGURED",
    )


def _mcp_rejected(payload: Any) -> bool:
    """True when ServiceImmediately refused the call.

    The server always reports `isError == False`, so refusals are detected from
    the `Error:` / `Denied:` message prefix by `mcp_client.is_rejection`.
    """
    return isinstance(payload, dict) and payload.get("status") == "REJECTED"


def _rejection(payload: Any, caller_id: str, tool: str, code: str) -> dict[str, Any]:
    """Surface a ServiceImmediately-side refusal as a structured denial (audited)."""
    message = str(_result_of(payload))
    log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name=tool,
        target_system="ServiceImmediately",
        action_type="WRITE",
        decision="DENY",
        deny_reason=code,
        request_summary={"mcp_message": message},
    )
    return {
        "status": "DENIED",
        "code": code,
        "system": "ServiceImmediately",
        "message": message,
        "detail": message,
    }


def _degraded(payload: dict[str, Any], caller_id: str, tool: str) -> dict[str, Any]:
    """Convert an MCP transport failure into graceful degradation (SDD §5.5)."""
    log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name=tool,
        target_system="ServiceImmediately",
        action_type="READ",
        decision="DENY",
        deny_reason=payload.get("code", "MCP_UNAVAILABLE"),
        request_summary={"mcp_message": payload.get("message")},
    )
    return {
        "status": "SERVICE_UNAVAILABLE",
        "code": payload.get("code", "MCP_UNAVAILABLE"),
        "system": "ServiceImmediately",
        "message": (
            "ServiceImmediately (ITSM) に現在接続できません。時間をおいて再度お試しいただくか、"
            "IT サービスデスクへ直接お問い合わせください。"
        ),
        "detail": payload.get("message"),
    }


def _extract_tickets(payload: Any) -> list[dict[str, Any]]:
    """Normalize a ticket payload into a list of ticket dicts.

    `list_tickets` and `create_ticket` both return a JSON document encoded as a
    string, which `mcp_client` has already decoded.
    """
    result = _result_of(payload)
    if isinstance(result, list):
        return [t for t in result if isinstance(t, dict)]
    if isinstance(result, dict) and result.get("ticket_id"):
        return [result]
    return []


def _ticket_id_of(t: dict[str, Any]) -> str:
    return str(t.get("ticket_id") or t.get("number") or t.get("id") or "")


def _status_of(t: dict[str, Any]) -> str:
    return str(t.get("status") or t.get("state") or "")


def _normalize_priority(priority: Any) -> tuple[str | None, str | None]:
    """Return (mcp_priority_label, error_message)."""
    if isinstance(priority, int):
        if priority not in PRIORITY_LABELS:
            return (
                None,
                f"優先度は 1〜4 の整数、または '1 - Critical' 形式の文字列で指定してください（入力: {priority}）。",
            )
        return PRIORITY_LABELS[priority], None

    text = str(priority).strip()
    if text in PRIORITY_LABELS.values():
        return text, None
    if text.isdigit() and int(text) in PRIORITY_LABELS:
        return PRIORITY_LABELS[int(text)], None
    for label in PRIORITY_LABELS.values():
        if text.lower() == label.split(" - ")[1].lower():
            return label, None
    return (
        None,
        f"優先度 '{priority}' は無効です。1〜4 または '1 - Critical' / '2 - High' / '3 - Moderate' / '4 - Low' を指定してください。",
    )


# ══════════════════════════════════════════════════════════════════════════
# READ TOOLS
# ══════════════════════════════════════════════════════════════════════════


def list_tickets(
    employee_id: str = "EMP-791", caller_id: str = "EMP-791"
) -> dict[str, Any]:
    """List all ServiceImmediately incident tickets requested by an employee, via MCP.

    Enforces Principle P6 (RBAC Data Isolation). ServiceImmediately independently
    re-verifies caller-context ownership.

    Args:
        employee_id: Employee ID whose tickets to list (default: 'EMP-791').
        caller_id: Authenticated caller employee ID (default: 'EMP-791').

    Returns:
        List of incident tickets with their IDs, categories, priorities, and statuses.
    """
    violation = enforce_data_isolation(
        caller_id, employee_id, "list_tickets", "ServiceImmediately"
    )
    if violation:
        return violation

    payload = mcp_call_sync(SERVER, "list_tickets", {"employee_id": employee_id})
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "list_tickets")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "list_tickets", "ITSM_ACCESS_DENIED")

    tickets = _extract_tickets(payload)
    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="list_tickets",
        target_system="ServiceImmediately",
        action_type="READ",
        decision="ALLOW",
        request_summary={
            "employee_id": employee_id,
            "count": len(tickets),
            "via": "mcp:list_tickets",
        },
    )
    return {
        "status": "SUCCESS",
        "system": "ServiceImmediately",
        "source": "mcp",
        "employee_id": employee_id,
        "tickets": tickets,
        "count": len(tickets),
        "audit_event_id": audit_id,
    }


def get_ticket(ticket_id: str, caller_id: str = "EMP-791") -> dict[str, Any]:
    """Retrieve details and comment timeline of a ServiceImmediately ticket, via MCP.

    Looks the ticket up within the caller's own ticket list, which enforces
    Principle P6 (RBAC Data Isolation) — a ticket owned by another employee is
    not visible and returns TICKET_NOT_FOUND.

    Args:
        ticket_id: Ticket identifier (e.g., 'INC0010001').
        caller_id: Authenticated caller employee ID (default: 'EMP-791').

    Returns:
        Ticket details including status, priority, category, description, and comments.
    """
    payload = mcp_call_sync(SERVER, "list_tickets", {"employee_id": caller_id})
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "get_ticket")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "get_ticket", "ITSM_ACCESS_DENIED")

    wanted = str(ticket_id).strip().lower()
    for t in _extract_tickets(payload):
        if _ticket_id_of(t).lower() == wanted:
            audit_id = log_audit_event(
                actor_id=caller_id,
                target_employee_id=caller_id,
                tool_name="get_ticket",
                target_system="ServiceImmediately",
                action_type="READ",
                decision="ALLOW",
                request_summary={"ticket_id": ticket_id, "via": "mcp:list_tickets"},
            )
            return {
                "status": "SUCCESS",
                "system": "ServiceImmediately",
                "source": "mcp",
                "ticket": t,
                "audit_event_id": audit_id,
            }

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name="get_ticket",
        target_system="ServiceImmediately",
        action_type="READ",
        decision="DENY",
        deny_reason="TICKET_NOT_FOUND_OR_NOT_OWNED",
        request_summary={"ticket_id": ticket_id},
    )
    return {
        "status": "ERROR",
        "code": "TICKET_NOT_FOUND",
        "message": (
            f"チケット '{ticket_id}' は見つかりませんでした。"
            "存在しないか、他の従業員が所有しているため参照できません（Principle P6）。"
        ),
        "audit_event_id": audit_id,
    }


# ══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS
# ══════════════════════════════════════════════════════════════════════════


def create_incident(
    employee_id: str = "EMP-791",
    category: str = "IT_Support",
    short_description: str = "",
    priority: int = 3,
    assignment_group: str = "Service Desk",
    amount_usd: float = 0.0,
    is_home_office_equipment: bool = False,
    work_arrangement: str = "",
    caller_id: str = "EMP-791",
    user_confirmed: bool = False,
    bypass_hitl_for_saga: bool = False,
) -> dict[str, Any]:
    """Create a new incident or service request in ServiceImmediately (ITSM) via MCP.

    Guardrails evaluated locally BEFORE the MCP call:
    - Principle P6: RBAC data isolation (`caller_id == employee_id`)
    - Guardrail G-ITSM-2: priority must resolve to 1 (Critical), 2 (High), 3 (Moderate), or 4 (Low).
      Critical priority additionally requires an outage/crash/downtime keyword in the description.
    - Guardrail G-ITSM-3: rejects a duplicate open ('New' / 'In Progress') ticket with the same
      category and description.
    - Policy Section 5.4 (Home Office Equipment Allowance): when the request is for home office
      equipment, the amount must not exceed $500.00 USD, the category must be 'Facilities', and the
      employee's WorkWeek work arrangement must be 'Remote' or 'Hybrid'.
    - Principle P3: HITL confirmation required unless invoked by a confirmed Saga.

    Args:
        employee_id: Employee ID for whom the ticket is created (default: 'EMP-791').
        category: Ticket category: 'IT_Support', 'Hardware', 'Software', 'Facilities', 'Security', or 'HR'.
        short_description: Concise summary of the issue or request.
        priority: Priority as an integer 1 (Critical), 2 (High), 3 (Moderate), or 4 (Low).
        assignment_group: Target assignment group (default: 'Service Desk').
        amount_usd: Requested amount in USD when the ticket covers an allowance (default: 0.0).
        is_home_office_equipment: Set True when requesting Home Office Equipment under Section 5.4.
        work_arrangement: Employee's WorkWeek work arrangement ('Remote', 'Hybrid', 'On-site'); required for Section 5.4 eligibility checks.
        caller_id: Authenticated caller ID (default: 'EMP-791').
        user_confirmed: Set True ONLY after the user explicitly confirms ticket creation.
        bypass_hitl_for_saga: Internal flag used by the confirmed Saga orchestrator.

    Returns:
        Result dictionary with the created ticket, or a specific guardrail denial code.
    """
    violation = enforce_data_isolation(
        caller_id, employee_id, "create_incident", "ServiceImmediately"
    )
    if violation:
        return violation

    desc = short_description.strip()
    if not desc:
        return {
            "status": "ERROR",
            "code": "MISSING_DESCRIPTION",
            "message": "short_description は必須です。",
        }

    # ── G-ITSM-2: priority validation ──────────────────────────────────────
    mcp_priority, prio_error = _normalize_priority(priority)
    if prio_error:
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="create_incident",
            target_system="ServiceImmediately",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-ITSM-2_INVALID_PRIORITY",
            request_summary={"priority": priority},
        )
        return {
            "status": "DENIED",
            "code": "G-ITSM-2_INVALID_PRIORITY",
            "message": f"Guardrail G-ITSM-2 Violation: {prio_error}",
            "audit_event_id": audit_id,
        }

    # Critical priority requires an active-outage keyword (ServiceImmediately rule).
    if mcp_priority == "1 - Critical" and not any(
        k in desc.lower() for k in CRITICAL_KEYWORDS
    ):
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="create_incident",
            target_system="ServiceImmediately",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-ITSM-2_CRITICAL_REQUIRES_OUTAGE",
            request_summary={"priority": mcp_priority, "short_description": desc},
        )
        return {
            "status": "DENIED",
            "code": "G-ITSM-2_CRITICAL_REQUIRES_OUTAGE",
            "message": (
                "Guardrail G-ITSM-2 Violation: ' 1 - Critical' の起票は、実際のシステム停止・クラッシュ・"
                "サービス全断を示す記述が必要です。該当しない場合は '2 - High' 以下で起票してください。"
            ),
            "audit_event_id": audit_id,
        }

    if category not in VALID_CATEGORIES:
        return {
            "status": "DENIED",
            "code": "INVALID_CATEGORY",
            "message": f"category は {VALID_CATEGORIES} のいずれかを指定してください（入力: '{category}'）。",
        }

    # ── Policy Section 5.4: Home Office Equipment Allowance ────────────────
    combined = f"{desc}".lower()
    is_hoe = is_home_office_equipment or any(
        kw in combined
        for kw in (
            "home office equipment",
            "ergonomic chair",
            "standing desk",
            "home office allowance",
            "monitor allowance",
            "在宅勤務機器",
        )
    )

    if is_hoe:
        arrangement = work_arrangement.strip()
        if not arrangement:
            # NOTE: WorkWeek's `get_personal_info` exposes only address and phone --
            # there is no work-arrangement field on this backend. We therefore cannot
            # infer eligibility, and the caller must pass `work_arrangement` explicitly.
            # When it is unknown we skip the Section 5.4 eligibility check rather than
            # guessing, and record that fact for the audit trail.
            arrangement = ""

        if arrangement and arrangement not in ("Remote", "Hybrid"):
            audit_id = log_audit_event(
                actor_id=caller_id,
                target_employee_id=employee_id,
                tool_name="create_incident",
                target_system="ServiceImmediately",
                action_type="WRITE",
                decision="DENY",
                deny_reason="POLICY_RULE_SECTION_5_4_INELIGIBLE_WORK_ARRANGEMENT",
                request_summary={"work_arrangement": arrangement},
            )
            return {
                "status": "DENIED",
                "code": "POLICY_RULE_SECTION_5_4_INELIGIBLE_WORK_ARRANGEMENT",
                "message": (
                    f"Policy Section 5.4 Violation: Home Office Equipment Allowance ($500 USD) is available "
                    f"only to employees with a 'Remote' or 'Hybrid' work arrangement in WorkWeek. "
                    f"Employee {employee_id} currently has work_arrangement='{arrangement}'."
                ),
                "citation": "Section 5.4 (Home Office Equipment Allowance)",
                "audit_event_id": audit_id,
            }

        if category != "Facilities":
            audit_id = log_audit_event(
                actor_id=caller_id,
                target_employee_id=employee_id,
                tool_name="create_incident",
                target_system="ServiceImmediately",
                action_type="WRITE",
                decision="DENY",
                deny_reason="POLICY_RULE_SECTION_5_4_WRONG_CATEGORY_MUST_BE_FACILITIES",
                request_summary={"category": category, "required": "Facilities"},
            )
            return {
                "status": "DENIED",
                "code": "POLICY_RULE_SECTION_5_4_WRONG_CATEGORY_MUST_BE_FACILITIES",
                "message": (
                    f"Policy Section 5.4 Violation: Home Office Equipment Allowance requests must be routed to "
                    f"the 'Facilities' category in ServiceImmediately (received category='{category}')."
                ),
                "citation": "Section 5.4 (Home Office Equipment Allowance)",
                "audit_event_id": audit_id,
            }

        if amount_usd > 500.0:
            audit_id = log_audit_event(
                actor_id=caller_id,
                target_employee_id=employee_id,
                tool_name="create_incident",
                target_system="ServiceImmediately",
                action_type="WRITE",
                decision="DENY",
                deny_reason="POLICY_RULE_SECTION_5_4_EXCEEDS_500_USD_CAP",
                request_summary={"amount_usd": amount_usd, "cap_usd": 500.0},
            )
            return {
                "status": "DENIED",
                "code": "POLICY_RULE_SECTION_5_4_EXCEEDS_500_USD_CAP",
                "message": (
                    f"Policy Section 5.4 Violation: Home Office Equipment Allowance has a strict maximum cap of "
                    f"$500.00 USD per eligible employee (requested: ${amount_usd:.2f} USD)."
                ),
                "citation": "Section 5.4 (Home Office Equipment Allowance)",
                "audit_event_id": audit_id,
            }

    # ── G-ITSM-3: duplicate open incident detection (live read) ────────────
    existing_payload = mcp_call_sync(
        SERVER, "list_tickets", {"employee_id": employee_id}
    )
    if not _mcp_failed(existing_payload):
        for t in _extract_tickets(existing_payload):
            if _status_of(t) not in ("New", "In Progress"):
                continue
            t_desc = (
                str(t.get("short_description") or t.get("description") or "")
                .strip()
                .lower()
            )
            t_cat = str(t.get("category") or "")
            if t_desc == desc.lower() and t_cat == category:
                audit_id = log_audit_event(
                    actor_id=caller_id,
                    target_employee_id=employee_id,
                    tool_name="create_incident",
                    target_system="ServiceImmediately",
                    action_type="WRITE",
                    decision="DENY",
                    deny_reason="G-ITSM-3_DUPLICATE_INCIDENT",
                    request_summary={
                        "existing_ticket_id": _ticket_id_of(t),
                        "short_description": desc,
                    },
                )
                return {
                    "status": "DENIED",
                    "code": "G-ITSM-3_DUPLICATE_INCIDENT",
                    "message": (
                        f"Guardrail G-ITSM-3 Violation: An open ticket with identical category ('{category}') "
                        f"and description already exists: {_ticket_id_of(t)} (status: {_status_of(t)})."
                    ),
                    "existing_ticket_id": _ticket_id_of(t),
                    "audit_event_id": audit_id,
                }

    # ── Principle P3: HITL confirmation gate ───────────────────────────────
    if not user_confirmed and not bypass_hitl_for_saga:
        log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="create_incident",
            target_system="ServiceImmediately",
            action_type="WRITE_PROPOSAL",
            decision="ALLOW",
            request_summary={
                "category": category,
                "priority": mcp_priority,
                "short_description": desc,
            },
        )
        return {
            "status": "CONFIRMATION_REQUIRED",
            "system": "ServiceImmediately",
            "message": "ガードレールを通過しました。チケットを起票する前に本人確認（HITL）が必要です。",
            "proposed_ticket": {
                "employee_id": employee_id,
                "category": category,
                "priority": mcp_priority,
                "short_description": desc,
                "assignment_group": assignment_group,
                **({"amount_usd": amount_usd} if amount_usd else {}),
            },
            "instruction_to_agent": (
                "Present the ticket summary to the user and ask for confirmation before calling "
                "create_incident with user_confirmed=True."
            ),
        }

    # ── Principle P2: idempotency ──────────────────────────────────────────
    idem_args = {
        "employee_id": employee_id,
        "category": category,
        "priority": mcp_priority,
        "short_description": desc,
    }
    idem_key = compute_idempotency_key(caller_id, "create_incident", idem_args)
    cached = check_idempotency(idem_key)
    if cached:
        return cached

    # ── MCP write ──────────────────────────────────────────────────────────
    payload = mcp_call_sync(
        SERVER,
        "create_ticket",
        {
            "requested_by": employee_id,
            "category": category,
            "short_description": desc,
            "priority": mcp_priority,
            "assignment_group": assignment_group,
        },
    )
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "create_incident")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "create_incident", "ITSM_CREATE_REJECTED")

    # `create_ticket` echoes the full ticket back as a JSON document.
    created = _extract_tickets(payload)
    new_id = _ticket_id_of(created[0]) if created else ""

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="create_incident",
        target_system="ServiceImmediately",
        action_type="WRITE",
        decision="ALLOW",
        idempotency_key=idem_key,
        origin="HUMAN_CONFIRMED" if user_confirmed else "SAGA_ORCHESTRATOR",
        request_summary={**idem_args, "ticket_id": new_id, "via": "mcp:create_ticket"},
    )
    response = {
        "status": "SUCCESS",
        "system": "ServiceImmediately",
        "source": "mcp",
        "ticket_id": new_id,
        "employee_id": employee_id,
        "category": category,
        "priority": mcp_priority,
        "short_description": desc,
        "mcp_response": payload,
        "idempotency_key": idem_key,
        "audit_event_id": audit_id,
    }
    record_idempotency(idem_key, caller_id, "create_incident", response)
    return response


def add_ticket_comment(
    ticket_id: str,
    comment: str,
    caller_id: str = "EMP-791",
) -> dict[str, Any]:
    """Append a comment to a ServiceImmediately ticket's activity timeline, via MCP.

    Args:
        ticket_id: Target ticket ID (e.g., 'INC0010001').
        comment: Text comment to append to the timeline.
        caller_id: Authenticated caller employee ID, recorded as the comment author (default: 'EMP-791').

    Returns:
        Result dictionary confirming the comment was appended.
    """
    if not comment.strip():
        return {
            "status": "ERROR",
            "code": "EMPTY_COMMENT",
            "message": "コメント本文を入力してください。",
        }

    # Ownership pre-check (Principle P6) — get_ticket only resolves the caller's own tickets.
    owned = get_ticket(ticket_id, caller_id=caller_id)
    if owned.get("status") != "SUCCESS":
        return owned

    payload = mcp_call_sync(
        SERVER,
        "add_ticket_comment",
        {"ticket_id": ticket_id, "author": caller_id, "comment": comment},
    )
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "add_ticket_comment")
    if _mcp_rejected(payload):
        return _rejection(
            payload, caller_id, "add_ticket_comment", "ITSM_COMMENT_REJECTED"
        )

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name="add_ticket_comment",
        target_system="ServiceImmediately",
        action_type="WRITE",
        decision="ALLOW",
        request_summary={
            "ticket_id": ticket_id,
            "comment_length": len(comment),
            "via": "mcp:add_ticket_comment",
        },
    )
    return {
        "status": "SUCCESS",
        "system": "ServiceImmediately",
        "source": "mcp",
        "ticket_id": ticket_id,
        "mcp_response": payload,
        "audit_event_id": audit_id,
    }


def update_ticket_status(
    ticket_id: str,
    new_status: str,
    resolution_notes: str = "",
    caller_id: str = "EMP-791",
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Transition a ServiceImmediately ticket to a new lifecycle status, via MCP, enforcing G-ITSM-1.

    Guardrail G-ITSM-1 state machine (stricter than the ServiceImmediately backend):
    - From 'New': only 'In Progress' or 'Cancelled'.
      A direct 'New' -> 'Closed' or 'New' -> 'Resolved' transition is STRICTLY FORBIDDEN, so every
      incident retains a triage and resolution trail.
    - From 'In Progress': 'Resolved', 'Closed', or 'Cancelled'.
    - From 'Resolved': 'Closed' or 'In Progress'.
    - 'Closed' and 'Cancelled' are terminal and locked.

    Args:
        ticket_id: Ticket ID to update (e.g., 'INC0010001').
        new_status: Target status ('In Progress', 'Resolved', 'Closed', 'Cancelled').
        resolution_notes: Optional resolution or cancellation note.
        caller_id: Authenticated caller ID (default: 'EMP-791').
        user_confirmed: Set True ONLY after the user explicitly confirms the transition.

    Returns:
        Result indicating SUCCESS, CONFIRMATION_REQUIRED, or G-ITSM-1_INVALID_STATE_TRANSITION.
    """
    owned = get_ticket(ticket_id, caller_id=caller_id)
    if owned.get("status") != "SUCCESS":
        return owned

    current_status = _status_of(owned["ticket"]) or "New"
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())

    # ── G-ITSM-1 enforcement ───────────────────────────────────────────────
    if new_status not in allowed:
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=caller_id,
            tool_name="update_ticket_status",
            target_system="ServiceImmediately",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-ITSM-1_INVALID_STATE_TRANSITION",
            request_summary={
                "ticket_id": ticket_id,
                "from": current_status,
                "to": new_status,
            },
        )
        return {
            "status": "DENIED",
            "code": "G-ITSM-1_INVALID_STATE_TRANSITION",
            "message": (
                f"Guardrail G-ITSM-1 Violation: Invalid state transition for ticket {ticket_id} from "
                f"'{current_status}' directly to '{new_status}'. Allowed transitions from "
                f"'{current_status}' are: {sorted(allowed)}. A ticket in 'New' status cannot be directly "
                f"Closed without first moving to 'In Progress' and 'Resolved'."
            ),
            "current_status": current_status,
            "requested_status": new_status,
            "allowed_transitions": sorted(allowed),
            "audit_event_id": audit_id,
        }

    if not user_confirmed:
        log_audit_event(
            actor_id=caller_id,
            target_employee_id=caller_id,
            tool_name="update_ticket_status",
            target_system="ServiceImmediately",
            action_type="WRITE_PROPOSAL",
            decision="ALLOW",
            request_summary={
                "ticket_id": ticket_id,
                "from": current_status,
                "to": new_status,
            },
        )
        return {
            "status": "CONFIRMATION_REQUIRED",
            "system": "ServiceImmediately",
            "message": (
                f"チケット {ticket_id} を '{current_status}' から '{new_status}' へ遷移させる前に "
                "本人確認（HITL）が必要です。"
            ),
            "proposed_transition": {
                "ticket_id": ticket_id,
                "from_status": current_status,
                "to_status": new_status,
                "resolution_notes": resolution_notes,
            },
            "instruction_to_agent": (
                "Ask the user to confirm before calling update_ticket_status with user_confirmed=True."
            ),
        }

    payload = mcp_call_sync(
        SERVER,
        "update_ticket_status",
        {
            "ticket_id": ticket_id,
            "status": new_status,
            "resolution_notes": resolution_notes,
            "updated_by": caller_id,
        },
    )
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "update_ticket_status")
    if _mcp_rejected(payload):
        return _rejection(
            payload, caller_id, "update_ticket_status", "ITSM_TRANSITION_REJECTED"
        )

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name="update_ticket_status",
        target_system="ServiceImmediately",
        action_type="WRITE",
        decision="ALLOW",
        origin="HUMAN_CONFIRMED",
        request_summary={
            "ticket_id": ticket_id,
            "from": current_status,
            "to": new_status,
            "via": "mcp:update_ticket_status",
        },
    )
    return {
        "status": "SUCCESS",
        "system": "ServiceImmediately",
        "source": "mcp",
        "ticket_id": ticket_id,
        "previous_status": current_status,
        "new_status": new_status,
        "mcp_response": payload,
        "audit_event_id": audit_id,
    }
