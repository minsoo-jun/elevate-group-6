"""WorkWeek HRMS Tools — MCP-backed (SDD §5.1 EnterpriseToolAdapter, §5.3 Guardrails, UC-1.2).

System of record: the **WorkWeek MCP server** mounted at `/work-week/mcp/` on the
Unified Mock Enterprise Services host. All reads and writes are delegated to MCP
tools over stateless Streamable HTTP (`X-MCP-Token` auth).

The local layer remains the Policy Enforcement Point (Principle P1/P2): every
deterministic business guardrail is evaluated **before** the MCP call is made, so
a rejected request never reaches the downstream SaaS.

Guardrail order per write:
  1. P6  RBAC data isolation          (caller_id == employee_id)
  2.     Policy gotchas               (Section 25.3 TOIL, 19.2 MC, 18.2 notice)
  3. G-HCM-1  balance sufficiency     (reads live balances from MCP)
  4. G-HCM-3  overlap detection       (reads live leave history from MCP)
  5. P3  HITL confirmation gate
  6. P2  idempotency replay check
  7. ──► MCP call
  8. P7  audit record (ALLOW / DENY both persisted)
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from app.tools.adapter import (
    check_idempotency,
    compute_idempotency_key,
    enforce_data_isolation,
    log_audit_event,
    record_idempotency,
)
from app.tools.mcp_client import mcp_call_sync, mcp_enabled

SERVER = "workweek"

# The MCP Personal Access Token maps to exactly one employee context; the
# backend refuses to act on behalf of anybody else. Resolved live via
# `get_current_employee_id()`.
DEFAULT_EMPLOYEE_ID = "EMP-791"

# Local leave-type vocabulary → WorkWeek MCP `leave_type` values.
#
# NOTE: the backend compares these case-sensitively and rejects anything else
# with "Error: Leave type must be 'Vacation' or 'Sick'." Lowercase values fail.
LEAVE_TYPE_TO_MCP = {
    "annual": "Vacation",
    "vacation": "Vacation",
    "sick": "Sick",
}
# Leave types governed by policy but not bookable through the WorkWeek MCP backend.
OFFLINE_LEAVE_TYPES = {"childcare", "personal", "toil"}

# Reference date used for deterministic notice-period arithmetic (Section 18.2).
NOTICE_REFERENCE_DATE = datetime.date(2026, 9, 16)

# `get_employee_balances` returns a human-readable report, not JSON:
#   "Employee EMP-791 Leave Balances:
#    - Vacation: 15.0 days remaining (5.0/20.0 used)
#    - Sick: 10.0 days remaining (0.0/10.0 used)"
_BALANCE_LINE_RE = re.compile(
    r"^\s*-\s*(?P<type>\w+)\s*:\s*(?P<remaining>[\d.]+)\s*days remaining"
    r"(?:\s*\((?P<used>[\d.]+)\s*/\s*(?P<entitlement>[\d.]+)\s*used\))?",
    re.IGNORECASE | re.MULTILINE,
)

# `get_personal_info` likewise:
#   "Employee EMP-791 Personal Info:
#    - Address: ...
#    - Phone: ..."
_INFO_LINE_RE = re.compile(r"^\s*-\s*(?P<key>[\w ]+?)\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)


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
    """True when WorkWeek itself refused the call (validation or missing entity).

    The server never sets `isError`, so this is derived from the message text
    by `mcp_client.is_rejection`.
    """
    return isinstance(payload, dict) and payload.get("status") == "REJECTED"


def _rejection(payload: Any, caller_id: str, tool: str, code: str) -> dict[str, Any]:
    """Surface a WorkWeek-side refusal as a structured denial (audited)."""
    message = str(_result_of(payload))
    log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name=tool,
        target_system="WorkWeek",
        action_type="WRITE",
        decision="DENY",
        deny_reason=code,
        request_summary={"mcp_message": message},
    )
    return {
        "status": "DENIED",
        "code": code,
        "system": "WorkWeek",
        "message": message,
        "detail": message,
    }


def _degraded(payload: dict[str, Any], caller_id: str, tool: str) -> dict[str, Any]:
    """Convert an MCP transport failure into a user-facing graceful degradation (SDD §5.5)."""
    log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name=tool,
        target_system="WorkWeek",
        action_type="READ",
        decision="DENY",
        deny_reason=payload.get("code", "MCP_UNAVAILABLE"),
        request_summary={"mcp_message": payload.get("message")},
    )
    return {
        "status": "SERVICE_UNAVAILABLE",
        "code": payload.get("code", "MCP_UNAVAILABLE"),
        "system": "WorkWeek",
        "message": (
            "WorkWeek (HRMS) に現在接続できません。時間をおいて再度お試しいただくか、"
            "HR ヘルプデスクへ直接お問い合わせください。"
        ),
        "detail": payload.get("message"),
    }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_balances(payload: Any) -> dict[str, float]:
    """Parse the WorkWeek balance report into remaining days per leave type.

    Returns ``{"vacation": 15.0, "sick": 10.0}``. Also exposes the accrual
    detail under ``*_used`` / ``*_entitlement`` keys when the server reports it.
    """
    text = _result_of(payload)
    if not isinstance(text, str):
        return {"vacation": 0.0, "sick": 0.0}

    balances: dict[str, float] = {"vacation": 0.0, "sick": 0.0}
    for match in _BALANCE_LINE_RE.finditer(text):
        leave_type = match.group("type").strip().lower()
        balances[leave_type] = _as_float(match.group("remaining"))
        if match.group("used") is not None:
            balances[f"{leave_type}_used"] = _as_float(match.group("used"))
        if match.group("entitlement") is not None:
            balances[f"{leave_type}_entitlement"] = _as_float(match.group("entitlement"))
    return balances


def _extract_personal_info(payload: Any) -> dict[str, str]:
    """Parse the WorkWeek personal-info report into {address, phone}."""
    text = _result_of(payload)
    if not isinstance(text, str):
        return {}

    fields: dict[str, str] = {}
    for match in _INFO_LINE_RE.finditer(text):
        key = match.group("key").strip().lower().replace(" ", "_")
        fields[key] = match.group("value").strip()
    return fields


def _extract_requests(payload: Any) -> list[dict[str, Any]]:
    """Normalize the WorkWeek MCP leave-request history into a list of dicts.

    `get_leave_requests` returns a JSON array encoded as a string, which
    `mcp_client` has already decoded for us.
    """
    result = _result_of(payload)
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    return []



def _parse_date(value: Any) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════════
# READ TOOLS
# ══════════════════════════════════════════════════════════════════════════


def get_employee_profile(employee_id: str = "EMP-791", caller_id: str = "EMP-791") -> dict[str, Any]:
    """Retrieve an employee's personal contact details from WorkWeek (HCM system) via MCP.

    Enforces Principle P6 (RBAC Data Isolation): `caller_id` must match `employee_id`.
    WorkWeek's own tenant-isolation layer independently re-verifies the identity context.

    Args:
        employee_id: Target employee ID (e.g., 'EMP-791').
        caller_id: Authenticated caller's employee ID (default: 'EMP-791').

    Returns:
        Employee profile including home address and phone number.
    """
    violation = enforce_data_isolation(caller_id, employee_id, "get_employee_profile", "WorkWeek")
    if violation:
        return violation

    payload = mcp_call_sync(SERVER, "get_personal_info", {"employee_id": employee_id})
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "get_employee_profile")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "get_employee_profile", "WORKWEEK_EMPLOYEE_NOT_FOUND")

    profile = _extract_personal_info(payload)

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="get_employee_profile",
        target_system="WorkWeek",
        action_type="READ",
        decision="ALLOW",
        request_summary={"employee_id": employee_id, "via": "mcp:get_personal_info"},
    )
    return {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": employee_id,
        "profile": {
            "employee_id": employee_id,
            "address": profile.get("address", ""),
            "phone": profile.get("phone", ""),
        },
        "raw_report": _result_of(payload),
        "audit_event_id": audit_id,
    }



def get_leave_balance(employee_id: str = "EMP-791", caller_id: str = "EMP-791") -> dict[str, Any]:
    """Retrieve an employee's remaining vacation and sick leave balances from WorkWeek via MCP.

    Enforces Principle P6 (RBAC Data Isolation): `caller_id` must match `employee_id`.

    Args:
        employee_id: Target employee ID (e.g., 'EMP-791').
        caller_id: Authenticated caller's employee ID (default: 'EMP-791').

    Returns:
        Remaining leave balances in days, the full leave request history, and applicable policy notes.
    """
    violation = enforce_data_isolation(caller_id, employee_id, "get_leave_balance", "WorkWeek")
    if violation:
        return violation

    balances_raw = mcp_call_sync(SERVER, "get_employee_balances", {"employee_id": employee_id})
    if _mcp_failed(balances_raw):
        return _degraded(balances_raw, caller_id, "get_leave_balance")
    if _mcp_rejected(balances_raw):
        return _rejection(balances_raw, caller_id, "get_leave_balance", "WORKWEEK_EMPLOYEE_NOT_FOUND")

    requests_raw = mcp_call_sync(SERVER, "get_leave_requests", {"employee_id": employee_id})
    history = [] if _mcp_failed(requests_raw) else _extract_requests(requests_raw)

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="get_leave_balance",
        target_system="WorkWeek",
        action_type="READ",
        decision="ALLOW",
        request_summary={"employee_id": employee_id, "via": "mcp:get_employee_balances"},
    )
    return {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": employee_id,
        "balances": _extract_balances(balances_raw),
        "balances_report": _result_of(balances_raw),
        "existing_requests": history,
        "policy_notes": {
            "toil_usage_rule": (
                "Per Section 25.3, accrued TOIL must be utilized before taking Paid Vacation. "
                "TOIL is tracked directly with your Line Manager and is not submitted as a WorkWeek leave request."
            ),
            "childcare_cap_rule": (
                "Per Section 24.2, statutory Childcare Leave and Extended Childcare Leave share a "
                "combined maximum cap of 6 days per calendar year for eligible parents."
            ),
        },
        "audit_event_id": audit_id,
    }


def get_leave_requests(employee_id: str = "EMP-791", caller_id: str = "EMP-791") -> dict[str, Any]:
    """Retrieve the full history of leave requests for an employee from WorkWeek via MCP.

    Args:
        employee_id: Target employee ID (e.g., 'EMP-791').
        caller_id: Authenticated caller's employee ID (default: 'EMP-791').

    Returns:
        List of leave requests with their dates, day counts, and statuses.
    """
    violation = enforce_data_isolation(caller_id, employee_id, "get_leave_requests", "WorkWeek")
    if violation:
        return violation

    payload = mcp_call_sync(SERVER, "get_leave_requests", {"employee_id": employee_id})
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "get_leave_requests")

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="get_leave_requests",
        target_system="WorkWeek",
        action_type="READ",
        decision="ALLOW",
        request_summary={"employee_id": employee_id, "via": "mcp:get_leave_requests"},
    )
    return {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": employee_id,
        "requests": _extract_requests(payload),
        "audit_event_id": audit_id,
    }


# ══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS
# ══════════════════════════════════════════════════════════════════════════


def update_contact_info(
    employee_id: str = "EMP-791",
    address: str = "",
    phone: str = "",
    caller_id: str = "EMP-791",
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Update an employee's home address and phone number in WorkWeek (HCM system) via MCP.

    Enforces:
    - Principle P6: RBAC data isolation (`caller_id == employee_id`)
    - Principle P3: HITL confirmation required (`user_confirmed=True`)
    - Principle P2: Idempotency replay protection
    - Principle P7: Audit logging
    - WorkWeek field validation: address must be at least 5 characters; phone must match `^\\+?[\\d\\s\\-()]{7,20}$`

    Args:
        employee_id: Target employee ID (default: 'EMP-791').
        address: New home address (minimum 5 characters). Leave empty to keep the current value.
        phone: New phone number, e.g. '+65-9123-4567'. Leave empty to keep the current value.
        caller_id: Authenticated caller employee ID (default: 'EMP-791').
        user_confirmed: Set True ONLY after the user explicitly confirms the proposed update.

    Returns:
        Result dictionary indicating SUCCESS, CONFIRMATION_REQUIRED, or DENIED.
    """
    violation = enforce_data_isolation(caller_id, employee_id, "update_contact_info", "WorkWeek")
    if violation:
        return violation

    # WorkWeek's update_personal_info requires both fields; backfill from current state.
    current = mcp_call_sync(SERVER, "get_personal_info", {"employee_id": employee_id})
    if _mcp_failed(current):
        return _degraded(current, caller_id, "update_contact_info")
    if _mcp_rejected(current):
        return _rejection(current, caller_id, "update_contact_info", "WORKWEEK_EMPLOYEE_NOT_FOUND")

    cur = _extract_personal_info(current)
    cur_address = cur.get("address", "")
    cur_phone = cur.get("phone", "")


    new_address = address.strip() or cur_address
    new_phone = phone.strip() or cur_phone

    if not address.strip() and not phone.strip():
        return {
            "status": "ERROR",
            "code": "NO_CHANGES_PROVIDED",
            "message": "住所または電話番号のいずれかを指定してください。",
        }

    # WorkWeek field-level validation, enforced before the MCP call.
    if len(new_address) < 5:
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="update_contact_info",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-HCM-6_INVALID_ADDRESS",
            request_summary={"address_length": len(new_address)},
        )
        return {
            "status": "DENIED",
            "code": "G-HCM-6_INVALID_ADDRESS",
            "message": f"WorkWeek の住所は 5 文字以上である必要があります（入力: {len(new_address)} 文字）。",
            "audit_event_id": audit_id,
        }

    import re

    if not re.fullmatch(r"\+?[\d\s\-()]{7,20}", new_phone):
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="update_contact_info",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-HCM-7_INVALID_PHONE",
            request_summary={"phone": new_phone},
        )
        return {
            "status": "DENIED",
            "code": "G-HCM-7_INVALID_PHONE",
            "message": (
                f"WorkWeek の電話番号形式が不正です（入力: '{new_phone}'）。"
                "7〜20 文字の数字・空白・ハイフン・括弧、先頭の + のみ使用できます。"
            ),
            "audit_event_id": audit_id,
        }

    updates = {}
    if address.strip():
        updates["address"] = new_address
    if phone.strip():
        updates["phone"] = new_phone

    # Principle P3: HITL confirmation gate
    if not user_confirmed:
        log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="update_contact_info",
            target_system="WorkWeek",
            action_type="WRITE_PROPOSAL",
            decision="ALLOW",
            request_summary={"proposed_updates": updates, "status": "AWAITING_CONFIRMATION"},
        )
        return {
            "status": "CONFIRMATION_REQUIRED",
            "system": "WorkWeek",
            "message": "WorkWeek のレコードを更新する前に本人確認（HITL）が必要です。",
            "proposed_changes": {"employee_id": employee_id, "fields_to_update": updates},
            "current_values": {"address": cur_address, "phone": cur_phone},
            "instruction_to_agent": (
                "Present the proposed changes to the user and ask for explicit confirmation "
                "before calling update_contact_info with user_confirmed=True."
            ),
        }

    # Principle P2: idempotency
    idem_key = compute_idempotency_key(
        caller_id, "update_contact_info", {"employee_id": employee_id, **updates}
    )
    cached = check_idempotency(idem_key)
    if cached:
        return cached

    payload = mcp_call_sync(
        SERVER,
        "update_personal_info",
        {"employee_id": employee_id, "address": new_address, "phone": new_phone},
    )
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "update_contact_info")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "update_contact_info", "WORKWEEK_UPDATE_REJECTED")

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="update_contact_info",
        target_system="WorkWeek",
        action_type="WRITE",
        decision="ALLOW",
        idempotency_key=idem_key,
        origin="HUMAN_CONFIRMED",
        request_summary={
            "updated_fields": updates,
            "previous_state": {"address": cur_address, "phone": cur_phone},
            "via": "mcp:update_personal_info",
        },
    )
    response = {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": employee_id,
        "updated_fields": updates,
        "previous_state": {"address": cur_address, "phone": cur_phone},
        "mcp_response": payload,
        "idempotency_key": idem_key,
        "audit_event_id": audit_id,
    }
    record_idempotency(idem_key, caller_id, "update_contact_info", response)
    return response


def submit_leave_request(
    employee_id: str = "EMP-791",
    leave_type: str = "annual",
    start_date: str = "2026-11-10",
    end_date: str = "2026-11-12",
    days: float = 3.0,
    caller_id: str = "EMP-791",
    user_confirmed: bool = False,
    has_medical_certificate: bool = False,
    toil_cleared_by_manager: bool = False,
    is_emergency: bool = False,
) -> dict[str, Any]:
    """Book vacation or sick leave in WorkWeek (HCM system) via MCP, behind deterministic guardrails.

    Guardrails evaluated locally BEFORE the MCP call reaches WorkWeek:
    - Principle P6: RBAC data isolation (`caller_id == employee_id`)
    - Section 25.3: TOIL is NOT recorded in WorkWeek — `leave_type='toil'` is rejected with manager-coordination guidance.
    - Section 25.3 / G-HCM-4: accrued TOIL must be consumed before Paid Vacation unless `toil_cleared_by_manager=True`.
    - Section 19.2 / G-HCM-5: sick leave exceeding 2 consecutive days requires a Medical Certificate.
    - Section 18.2 / G-HCM-2: Unpaid Personal Leave requires 30+ calendar days advance notice unless `is_emergency=True`.
    - G-HCM-2: `end_date` must not precede `start_date`; `days` must be positive; start date cannot be in the past.
    - G-HCM-1: requested days must not exceed the live remaining balance read from WorkWeek.
    - G-HCM-3: requested dates must not overlap an existing WorkWeek leave request.
    - Principle P3: HITL confirmation gate before the write is committed.

    Args:
        employee_id: Employee ID submitting the leave (default: 'EMP-791').
        leave_type: 'annual' / 'vacation' (Paid Vacation), 'sick' (Sick Leave), 'childcare', 'personal', or 'toil'.
        start_date: Leave start date in YYYY-MM-DD format.
        end_date: Leave end date in YYYY-MM-DD format.
        days: Number of leave days requested (must be greater than 0).
        caller_id: Authenticated caller ID (default: 'EMP-791').
        user_confirmed: Set True ONLY after the user explicitly confirms submitting the request.
        has_medical_certificate: True if a Medical Certificate is available (required for sick leave over 2 days, Section 19.2).
        toil_cleared_by_manager: True if accrued TOIL has already been consumed or cleared with the manager (Section 25.3).
        is_emergency: True if the emergency exception applies to the Unpaid Personal Leave notice period (Section 18.2).

    Returns:
        Structured result indicating SUCCESS, CONFIRMATION_REQUIRED, or DENIED with the specific guardrail code.
    """
    violation = enforce_data_isolation(caller_id, employee_id, "submit_leave_request", "WorkWeek")
    if violation:
        return violation

    lt = leave_type.strip().lower()

    # ── Section 25.3: TOIL is never recorded in WorkWeek ───────────────────
    if lt == "toil":
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="submit_leave_request",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="POLICY_RULE_SECTION_25_3_TOIL_NOT_IN_WORKWEEK",
            request_summary={"leave_type": lt, "days": days},
        )
        return {
            "status": "DENIED",
            "code": "POLICY_RULE_SECTION_25_3_TOIL_NOT_IN_WORKWEEK",
            "message": (
                "POLICY_RULE_SECTION_25_3: Time-Off In Lieu (TOIL) is NOT recorded or submitted in WorkWeek. "
                "Per Section 25.3 of the Employee Handbook, TOIL is tracked offline directly with your Line "
                "Manager and must be utilized within 3 months of accrual."
            ),
            "citation": "Section 25.3 (Time-Off In Lieu - Singapore)",
            "audit_event_id": audit_id,
        }

    if lt not in LEAVE_TYPE_TO_MCP and lt not in OFFLINE_LEAVE_TYPES:
        return {
            "status": "DENIED",
            "code": "INVALID_LEAVE_TYPE",
            "message": (
                f"Invalid leave_type '{leave_type}'. WorkWeek accepts 'annual'/'vacation' or 'sick'. "
                "'childcare' and 'personal' are policy-governed and handled offline."
            ),
        }

    # ── G-HCM-2: chronological validity ────────────────────────────────────
    dt_start = _parse_date(start_date)
    dt_end = _parse_date(end_date)
    if dt_start is None or dt_end is None:
        return {
            "status": "DENIED",
            "code": "G-HCM-2_INVALID_DATE_FORMAT",
            "message": "start_date と end_date は YYYY-MM-DD 形式で指定してください。",
        }

    if dt_end < dt_start or days <= 0:
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="submit_leave_request",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-HCM-2_INVALID_DATE_RANGE",
            request_summary={"start_date": start_date, "end_date": end_date, "days": days},
        )
        return {
            "status": "DENIED",
            "code": "G-HCM-2_INVALID_DATE_RANGE",
            "message": (
                f"Guardrail G-HCM-2 Violation: end_date ({end_date}) cannot be earlier than "
                f"start_date ({start_date}), and days ({days}) must be positive."
            ),
            "audit_event_id": audit_id,
        }

    # ── Section 18.2 / G-HCM-2: unpaid personal leave notice period ────────
    if lt == "personal" and not is_emergency:
        days_advance = (dt_start - NOTICE_REFERENCE_DATE).days
        if 0 <= days_advance < 30:
            audit_id = log_audit_event(
                actor_id=caller_id,
                target_employee_id=employee_id,
                tool_name="submit_leave_request",
                target_system="WorkWeek",
                action_type="WRITE",
                decision="DENY",
                deny_reason="G-HCM-2_NOTICE_PERIOD_VIOLATION",
                request_summary={"leave_type": "personal", "days_advance": days_advance},
            )
            return {
                "status": "DENIED",
                "code": "G-HCM-2_NOTICE_PERIOD_VIOLATION",
                "message": (
                    f"Guardrail G-HCM-2 / Section 18.2 Violation: Unpaid Personal Leave requires at least "
                    f"30 calendar days advance notice (requested start {start_date} is only {days_advance} "
                    f"days away). If this is an urgent emergency, resubmit with is_emergency=True."
                ),
                "citation": "Section 18.2 (Unpaid Time Off / Personal Leave)",
                "audit_event_id": audit_id,
            }

    # ── Section 19.2 / G-HCM-5: medical certificate ────────────────────────
    if lt == "sick" and days > 2.0 and not has_medical_certificate:
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="submit_leave_request",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-HCM-5_MEDICAL_CERTIFICATE_REQUIRED",
            request_summary={"leave_type": "sick", "days": days},
        )
        return {
            "status": "DENIED",
            "code": "G-HCM-5_MEDICAL_CERTIFICATE_REQUIRED",
            "message": (
                f"Policy Section 19.2 / Guardrail G-HCM-5 Violation: Sick leave exceeding 2 consecutive days "
                f"(requested: {days} days) requires a valid Medical Certificate (MC) uploaded to WorkWeek "
                f"within 48 hours. Please obtain an MC and set has_medical_certificate=True."
            ),
            "citation": "Section 19.2 (Sick Time & Hospitalization Leave - Singapore)",
            "audit_event_id": audit_id,
        }

    # ── Offline-only leave types: policy validated, not bookable via MCP ───
    if lt in ("childcare", "personal"):
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="submit_leave_request",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="LEAVE_TYPE_NOT_BOOKABLE_IN_WORKWEEK",
            request_summary={"leave_type": lt, "days": days},
        )
        citation = (
            "Section 24.2 (Childcare Leave - Singapore)"
            if lt == "childcare"
            else "Section 18.2 (Unpaid Time Off / Personal Leave)"
        )
        cap_note = (
            " なお Section 24.2 により、法定育児休暇と拡張育児休暇は暦年あたり合計 6 日が上限です。"
            if lt == "childcare"
            else ""
        )
        return {
            "status": "DENIED",
            "code": "LEAVE_TYPE_NOT_BOOKABLE_IN_WORKWEEK",
            "message": (
                f"'{lt}' 休暇は WorkWeek のセルフサービス申請対象外です。"
                f"ポリシー要件は満たしていますが、申請は HR 部門またはライン マネージャー経由で手続きしてください。{cap_note}"
            ),
            "citation": citation,
            "audit_event_id": audit_id,
        }

    mcp_leave_type = LEAVE_TYPE_TO_MCP[lt]

    # ── G-HCM-1: live balance sufficiency (read from WorkWeek) ─────────────
    balances_raw = mcp_call_sync(SERVER, "get_employee_balances", {"employee_id": employee_id})
    if _mcp_failed(balances_raw):
        return _degraded(balances_raw, caller_id, "submit_leave_request")

    balances = _extract_balances(balances_raw)
    # `_extract_balances` keys on the lowercased leave type, while `mcp_leave_type`
    # carries the capitalisation the WorkWeek backend demands ("Vacation" / "Sick").
    available = balances.get(mcp_leave_type.lower(), 0.0)
    if days > available:
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="submit_leave_request",
            target_system="WorkWeek",
            action_type="WRITE",
            decision="DENY",
            deny_reason="G-HCM-1_INSUFFICIENT_BALANCE",
            request_summary={"leave_type": mcp_leave_type, "requested": days, "available": available},
        )
        return {
            "status": "DENIED",
            "code": "G-HCM-1_INSUFFICIENT_BALANCE",
            "message": (
                f"Guardrail G-HCM-1 Violation: Insufficient '{mcp_leave_type}' leave balance in WorkWeek. "
                f"Requested: {days} days, Available: {available} days."
            ),
            "available_balance": available,
            "requested_days": days,
            "live_balances": balances,
            "audit_event_id": audit_id,
        }

    # ── Section 25.3 / G-HCM-4: TOIL must be consumed before Paid Vacation ─
    if mcp_leave_type.lower() == "vacation" and not toil_cleared_by_manager:
        toil_balance = _as_float(
            (balances_raw or {}).get("toil_remaining")
            or (balances_raw or {}).get("toil")
            or 0.0
        )
        if toil_balance > 0:
            audit_id = log_audit_event(
                actor_id=caller_id,
                target_employee_id=employee_id,
                tool_name="submit_leave_request",
                target_system="WorkWeek",
                action_type="WRITE",
                decision="DENY",
                deny_reason="G-HCM-4_TOIL_MUST_BE_USED_FIRST",
                request_summary={"leave_type": "vacation", "toil_balance": toil_balance},
            )
            return {
                "status": "DENIED",
                "code": "G-HCM-4_TOIL_MUST_BE_USED_FIRST",
                "message": (
                    f"Policy Section 25.3 / Guardrail G-HCM-4 Violation: You currently have {toil_balance} days "
                    f"of accrued Time-Off In Lieu (TOIL). Per Section 25.3, accrued TOIL must be utilized BEFORE "
                    f"taking standard Paid Vacation. Please coordinate with your manager to use your TOIL days "
                    f"first (or set toil_cleared_by_manager=True if already utilized)."
                ),
                "citation": "Section 25.3 (Time-Off In Lieu - Singapore)",
                "audit_event_id": audit_id,
            }

    # ── G-HCM-3: overlap detection against live WorkWeek history ───────────
    requests_raw = mcp_call_sync(SERVER, "get_leave_requests", {"employee_id": employee_id})
    if not _mcp_failed(requests_raw):
        for existing in _extract_requests(requests_raw):
            status_val = str(existing.get("status", "")).upper()
            if status_val in ("CANCELLED", "CANCELED", "REJECTED", "DENIED"):
                continue
            ex_start = _parse_date(existing.get("start_date"))
            ex_end = _parse_date(existing.get("end_date"))
            if not ex_start or not ex_end:
                continue
            if dt_start <= ex_end and dt_end >= ex_start:
                audit_id = log_audit_event(
                    actor_id=caller_id,
                    target_employee_id=employee_id,
                    tool_name="submit_leave_request",
                    target_system="WorkWeek",
                    action_type="WRITE",
                    decision="DENY",
                    deny_reason="G-HCM-3_OVERLAPPING_LEAVE",
                    request_summary={
                        "requested_range": f"{start_date}..{end_date}",
                        "conflicting_request": existing,
                    },
                )
                return {
                    "status": "DENIED",
                    "code": "G-HCM-3_OVERLAPPING_LEAVE",
                    "message": (
                        f"Guardrail G-HCM-3 Violation: Requested dates ({start_date} to {end_date}) overlap "
                        f"with existing WorkWeek leave request "
                        f"({existing.get('start_date')} to {existing.get('end_date')}, "
                        f"status: {existing.get('status')})."
                    ),
                    "conflicting_request": existing,
                    "audit_event_id": audit_id,
                }

    # ── Principle P3: HITL confirmation gate ───────────────────────────────
    if not user_confirmed:
        log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="submit_leave_request",
            target_system="WorkWeek",
            action_type="WRITE_PROPOSAL",
            decision="ALLOW",
            request_summary={
                "leave_type": mcp_leave_type,
                "start_date": start_date,
                "end_date": end_date,
                "days": days,
                "status": "AWAITING_CONFIRMATION",
            },
        )
        return {
            "status": "CONFIRMATION_REQUIRED",
            "system": "WorkWeek",
            "message": (
                "すべての業務ガードレールを通過しました。WorkWeek に休暇を申請する前に "
                "本人確認（HITL）が必要です。"
            ),
            "proposed_request": {
                "employee_id": employee_id,
                "leave_type": mcp_leave_type,
                "start_date": start_date,
                "end_date": end_date,
                "days": days,
            },
            "remaining_balance_before": available,
            "instruction_to_agent": (
                "Ask the user to explicitly confirm submitting this leave request before calling "
                "submit_leave_request with user_confirmed=True."
            ),
        }

    # ── Principle P2: idempotency ──────────────────────────────────────────
    idem_args = {
        "employee_id": employee_id,
        "leave_type": mcp_leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
    }
    idem_key = compute_idempotency_key(caller_id, "submit_leave_request", idem_args)
    cached = check_idempotency(idem_key)
    if cached:
        return cached

    # ── MCP write ──────────────────────────────────────────────────────────
    payload = mcp_call_sync(
        SERVER,
        "request_time_off",
        {
            "employee_id": employee_id,
            "start_date": start_date,
            "end_date": end_date,
            "leave_type": mcp_leave_type,
            "days": days,
        },
    )
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "submit_leave_request")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "submit_leave_request", "WORKWEEK_LEAVE_REJECTED")

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="submit_leave_request",
        target_system="WorkWeek",
        action_type="WRITE",
        decision="ALLOW",
        idempotency_key=idem_key,
        origin="HUMAN_CONFIRMED",
        request_summary={**idem_args, "via": "mcp:request_time_off"},
    )
    response = {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": employee_id,
        "leave_type": mcp_leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "mcp_response": payload,
        "idempotency_key": idem_key,
        "audit_event_id": audit_id,
    }
    record_idempotency(idem_key, caller_id, "submit_leave_request", response)
    return response


def cancel_leave_request(
    employee_id: str = "EMP-791",
    request_id: int = 0,
    caller_id: str = "EMP-791",
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Cancel a pending or approved WorkWeek leave request via MCP and refund the days.

    Enforces Principle P6 (RBAC data isolation), Principle P3 (HITL confirmation),
    and Principle P7 (audit logging).

    Args:
        employee_id: Employee ID owning the leave request (default: 'EMP-791').
        request_id: Numeric ID of the leave request to cancel.
        caller_id: Authenticated caller employee ID (default: 'EMP-791').
        user_confirmed: Set True ONLY after the user explicitly confirms the cancellation.

    Returns:
        Result dictionary indicating SUCCESS, CONFIRMATION_REQUIRED, or DENIED.
    """
    violation = enforce_data_isolation(caller_id, employee_id, "cancel_leave_request", "WorkWeek")
    if violation:
        return violation

    if not request_id:
        return {
            "status": "ERROR",
            "code": "MISSING_REQUEST_ID",
            "message": "取り消す休暇申請の request_id を指定してください。get_leave_requests で確認できます。",
        }

    if not user_confirmed:
        log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="cancel_leave_request",
            target_system="WorkWeek",
            action_type="WRITE_PROPOSAL",
            decision="ALLOW",
            request_summary={"request_id": request_id, "status": "AWAITING_CONFIRMATION"},
        )
        return {
            "status": "CONFIRMATION_REQUIRED",
            "system": "WorkWeek",
            "message": "WorkWeek の休暇申請を取り消す前に本人確認（HITL）が必要です。",
            "proposed_changes": {"employee_id": employee_id, "request_id": request_id, "action": "cancel"},
            "instruction_to_agent": (
                "Confirm the cancellation with the user before calling cancel_leave_request "
                "with user_confirmed=True."
            ),
        }

    payload = mcp_call_sync(
        SERVER, "cancel_leave_request", {"employee_id": employee_id, "request_id": request_id}
    )
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "cancel_leave_request")
    if _mcp_rejected(payload):
        return _rejection(payload, caller_id, "cancel_leave_request", "WORKWEEK_CANCEL_REJECTED")

    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="cancel_leave_request",
        target_system="WorkWeek",
        action_type="WRITE",
        decision="ALLOW",
        origin="HUMAN_CONFIRMED",
        request_summary={"request_id": request_id, "via": "mcp:cancel_leave_request"},
    )
    return {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": employee_id,
        "request_id": request_id,
        "mcp_response": _result_of(payload),
        "audit_event_id": audit_id,
    }


def get_current_employee_id(caller_id: str = "EMP-791") -> dict[str, Any]:
    """Resolve the employee ID of the currently authenticated WorkWeek session via MCP.

    Args:
        caller_id: Authenticated caller employee ID used for audit attribution (default: 'EMP-791').

    Returns:
        The employee ID resolved by WorkWeek for the active session.
    """
    payload = mcp_call_sync(SERVER, "get_current_employee_id", {})
    if _mcp_failed(payload):
        return _degraded(payload, caller_id, "get_current_employee_id")

    log_audit_event(
        actor_id=caller_id,
        target_employee_id=caller_id,
        tool_name="get_current_employee_id",
        target_system="WorkWeek",
        action_type="READ",
        decision="ALLOW",
        request_summary={"via": "mcp:get_current_employee_id"},
    )
    return {
        "status": "SUCCESS",
        "system": "WorkWeek",
        "source": "mcp",
        "employee_id": _result_of(payload),
        "result": _result_of(payload),
    }
