"""Cross-System Saga Orchestrator & Compensating Transactions (SDD §3.5, §5.6, UC-2.x).

Coordinates multi-system transactions spanning WorkWeek (HCM) and ServiceImmediately (ITSM)
with automatic rollback compensation on partial failure.

Both systems are now MCP-backed and there is no shared database, so the saga cannot
rely on a transaction. Compensation is therefore an explicit *forward* correction:
we snapshot the pre-saga WorkWeek state, and on downstream failure we re-issue a
write that restores that snapshot.

Backend constraint that shapes this design: WorkWeek's `update_personal_info` only
accepts `address` and `phone`. There is **no work-arrangement field** on this backend,
so the requested arrangement is carried into the ServiceImmediately ticket (where
Section 5.4 eligibility is assessed) rather than persisted in the HCM record.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.tools.adapter import enforce_data_isolation, log_audit_event
from app.tools.hcm_tools import get_employee_profile, update_contact_info
from app.tools.itsm_tools import create_incident


def execute_remote_work_transition_saga(
    employee_id: str = "EMP-791",
    new_address: str = "22 Remote Ave, Singapore 567890",
    new_work_arrangement: str = "Remote",
    equipment_title: str = "Home Office Equipment Allowance - Ergonomic Setup",
    equipment_description: str = "Requesting monitor and chair under Section 5.4 Home Office Equipment policy.",
    equipment_amount_usd: float = 450.0,
    caller_id: str = "EMP-791",
    user_confirmed: bool = False,
    simulate_itsm_failure: bool = False,
) -> dict[str, Any]:
    """Execute a Cross-System Remote Work Transition Saga (UC-2.x) spanning WorkWeek (HCM) and ServiceImmediately (ITSM).

    Workflow Steps:
    1. Update the employee's residential `address` in WorkWeek (HCM) via MCP.
    2. Create a Home Office Equipment Allowance ticket in ServiceImmediately (ITSM)
       under category='Facilities' (Section 5.4).
    3. Automatic Compensation Rollback: if Step 2 fails (e.g. ServiceImmediately outage
       or a Section 5.4 $500 USD cap violation), automatically revert Step 1 in WorkWeek
       back to the exact pre-saga address.

    Args:
        employee_id: Target employee ID (default: 'EMP-791').
        new_address: New remote work residential address.
        new_work_arrangement: Target arrangement ('Remote' or 'Hybrid'). Used for the
            Section 5.4 eligibility check on the ITSM ticket; WorkWeek cannot store it.
        equipment_title: Title for the ServiceImmediately Facilities equipment request.
        equipment_description: Detailed description of requested equipment.
        equipment_amount_usd: Requested allowance amount in USD (must be <= $500.00 per Section 5.4).
        caller_id: Authenticated caller employee ID (default: 'EMP-791').
        user_confirmed: Set True ONLY after the user explicitly confirms the multi-system Saga execution.
        simulate_itsm_failure: Set True to simulate a ServiceImmediately outage and verify the
            automatic WorkWeek rollback. No ticket is created when this is set.

    Returns:
        Structured Saga execution report showing final state (`SAGA_COMPLETED` or `SAGA_COMPENSATED_ROLLED_BACK`).
    """
    violation = enforce_data_isolation(
        caller_id, employee_id, "execute_remote_work_transition_saga", "CloudWorkflowsSaga"
    )
    if violation:
        return violation

    saga_id = f"SAGA-{uuid.uuid4().hex[:8].upper()}"

    if not user_confirmed:
        log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="execute_remote_work_transition_saga",
            target_system="CloudWorkflowsSaga",
            action_type="WRITE_PROPOSAL",
            decision="ALLOW",
            request_summary={
                "saga_id": saga_id,
                "step1_hcm": {"address": new_address, "work_arrangement": new_work_arrangement},
                "step2_itsm": {"category": "Facilities", "amount_usd": equipment_amount_usd},
            },
        )
        return {
            "status": "CONFIRMATION_REQUIRED",
            "saga_id": saga_id,
            "message": "Human-in-the-Loop (HITL) confirmation is required before executing this cross-system Saga.",
            "proposed_saga_plan": {
                "step_1_workweek_hcm": {
                    "action": "update_contact_info",
                    "employee_id": employee_id,
                    "new_address": new_address,
                    "new_work_arrangement": new_work_arrangement,
                },
                "step_2_service_immediately_itsm": {
                    "action": "create_incident",
                    "category": "Facilities",
                    "title": equipment_title,
                    "amount_usd": equipment_amount_usd,
                },
                "compensation_policy": "Automatic rollback of the WorkWeek address if ServiceImmediately ticket creation fails.",
            },
            "instruction_to_agent": "Present the 2-step Saga plan clearly to the user and ask for confirmation before calling with user_confirmed=True.",
        }

    # ── Capture pre-saga snapshot from WorkWeek for deterministic compensation ──
    snapshot = get_employee_profile(employee_id=employee_id, caller_id=caller_id)
    if snapshot.get("status") != "SUCCESS":
        return {
            "status": "SAGA_ABORTED_BEFORE_STEP_1",
            "saga_id": saga_id,
            "snapshot_result": snapshot,
            "message": "Saga aborted: the pre-saga WorkWeek snapshot could not be taken, so a rollback could not be guaranteed. No changes were made.",
        }

    snapshot_address = snapshot.get("profile", {}).get("address", "")
    snapshot_phone = snapshot.get("profile", {}).get("phone", "")

    # ── Step 1: WorkWeek (HCM) address update ──────────────────────────────
    hcm_res = update_contact_info(
        employee_id=employee_id,
        address=new_address,
        caller_id=caller_id,
        user_confirmed=True,
    )
    if hcm_res.get("status") != "SUCCESS":
        return {
            "status": "SAGA_ABORTED_AT_STEP_1",
            "saga_id": saga_id,
            "step_1_result": hcm_res,
            "message": "Saga aborted at Step 1 (WorkWeek update failed). No changes were committed.",
        }

    # ── Step 2: ServiceImmediately (ITSM) Facilities equipment ticket ──────
    if simulate_itsm_failure:
        itsm_res = {
            "status": "SERVICE_UNAVAILABLE",
            "code": "SIMULATED_ITSM_OUTAGE",
            "system": "ServiceImmediately",
            "message": "Simulated ServiceImmediately outage (injected for compensation testing).",
        }
    else:
        itsm_res = create_incident(
            employee_id=employee_id,
            category="Facilities",
            short_description=f"{equipment_title}: {equipment_description}",
            priority=3,
            amount_usd=equipment_amount_usd,
            is_home_office_equipment=True,
            work_arrangement=new_work_arrangement,
            caller_id=caller_id,
            user_confirmed=True,
            bypass_hitl_for_saga=True,
        )

    # ── Step 2 failed → compensating transaction (SDD §3.5, §5.6) ──────────
    if itsm_res.get("status") != "SUCCESS":
        compensation = update_contact_info(
            employee_id=employee_id,
            address=snapshot_address,
            phone=snapshot_phone,
            caller_id=caller_id,
            user_confirmed=True,
        )
        compensated = compensation.get("status") == "SUCCESS"

        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=employee_id,
            tool_name="execute_remote_work_transition_saga",
            target_system="CloudWorkflowsSaga",
            action_type="COMPENSATION_ROLLBACK",
            decision="ALLOW" if compensated else "DENY",
            deny_reason=itsm_res.get("code", "STEP_2_FAILED"),
            request_summary={
                "saga_id": saga_id,
                "step_2_failure": itsm_res,
                "reverted_to": {"address": snapshot_address, "phone": snapshot_phone},
                "compensation_status": compensation.get("status"),
            },
        )

        if not compensated:
            # Compensation itself failed: the systems are now inconsistent and a
            # human must intervene. Emit explicit manual remediation steps (SDD §5.6).
            return {
                "status": "SAGA_COMPENSATION_FAILED",
                "saga_id": saga_id,
                "failed_step": "Step 2 (ServiceImmediately ITSM Ticket Creation)",
                "failure_reason": itsm_res.get("message"),
                "compensation_result": compensation,
                "manual_remediation_steps": [
                    f"WorkWeek の従業員 {employee_id} の住所を「{snapshot_address}」に手動で戻してください。",
                    f"電話番号が変更されている場合は「{snapshot_phone}」に戻してください。",
                    f"対応後、Saga ID {saga_id} を添えて HR ヘルプデスクへ完了報告してください。",
                ],
                "user_guidance": (
                    "システム横断処理（Saga）のステップ2が失敗し、さらに自動ロールバックにも失敗しました。"
                    "データ不整合が残っているため、上記の手動対応手順に従ってください。"
                ),
                "audit_event_id": audit_id,
            }

        return {
            "status": "SAGA_COMPENSATED_ROLLED_BACK",
            "saga_id": saga_id,
            "failed_step": "Step 2 (ServiceImmediately ITSM Ticket Creation)",
            "failure_reason": itsm_res.get("message"),
            "failure_code": itsm_res.get("code"),
            "compensation_action": {
                "system": "WorkWeek",
                "status": "ROLLED_BACK_SUCCESSFULLY",
                "reverted_address": snapshot_address,
                "reverted_phone": snapshot_phone,
            },
            "user_guidance": (
                "システム横断処理（Saga）のステップ2（ServiceImmediatelyでの在宅勤務機器申請）が失敗したため、"
                "補償トランザクションによりWorkWeek上の住所変更を自動的にロールバック（元の状態へ復元）しました。"
                "データの不整合は発生していません。"
            ),
            "audit_event_id": audit_id,
        }

    # ── Both steps succeeded ───────────────────────────────────────────────
    audit_id = log_audit_event(
        actor_id=caller_id,
        target_employee_id=employee_id,
        tool_name="execute_remote_work_transition_saga",
        target_system="CloudWorkflowsSaga",
        action_type="WRITE",
        decision="ALLOW",
        origin="HUMAN_CONFIRMED",
        request_summary={
            "saga_id": saga_id,
            "workweek_status": "UPDATED",
            "service_immediately_ticket": itsm_res.get("ticket_id"),
        },
    )
    return {
        "status": "SAGA_COMPLETED",
        "saga_id": saga_id,
        "step_1_workweek": {
            "status": "SUCCESS",
            "employee_id": employee_id,
            "updated_address": new_address,
            "previous_address": snapshot_address,
            "requested_work_arrangement": new_work_arrangement,
            "note": "WorkWeek does not persist a work-arrangement field; it is recorded on the ITSM ticket.",
        },
        "step_2_service_immediately": {
            "status": "SUCCESS",
            "ticket_id": itsm_res.get("ticket_id"),
            "category": "Facilities",
            "amount_usd": equipment_amount_usd,
        },
        "audit_event_id": audit_id,
    }
