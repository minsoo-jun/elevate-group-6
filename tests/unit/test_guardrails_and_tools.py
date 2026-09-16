"""Unit Tests for HR Concierge Agent Tools, Guardrails, Policy Gotchas & Saga Compensation."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.tools.adapter import AUDIT_LOG_PATH, DB_PATH
from app.tools.hcm_tools import (
    get_employee_profile,
    get_leave_balance,
    submit_leave_request,
    update_contact_info,
)
from app.tools.itsm_tools import (
    add_ticket_comment,
    create_incident,
    get_ticket,
    update_ticket_status,
)
from app.tools.policy_tools import (
    list_policy_concepts,
    read_policy_concept,
    search_policy,
)
from app.tools.saga_tools import execute_remote_work_transition_saga


@pytest.fixture(autouse=True)
def clean_state():
    """Reset SQLite test database before each test for deterministic isolation."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    yield


def test_policy_search_and_citations():
    """Verify search_policy retrieves grounded sections with Section X.Y citations."""
    res = search_policy("host gift card $50 limit")
    assert res["status"] == "SUCCESS"
    assert len(res["results"]) > 0
    # Ensure Section 4.3 or commercial gifts is found
    combined = " ".join(r["content"].lower() for r in res["results"])
    assert "gift" in combined

    concepts = list_policy_concepts()
    assert concepts["status"] == "SUCCESS"
    assert concepts["total_sections"] > 10

    read_res = read_policy_concept("25")
    assert read_res["status"] == "SUCCESS"


def test_rbac_data_isolation_p6():
    """Verify Principle P6: EMP-1001 cannot access EMP-1002 profile or leave balances."""
    res = get_employee_profile(employee_id="EMP-1002", caller_id="EMP-1001")
    assert res["status"] == "DENIED"
    assert res["code"] == "RBAC_VIOLATION"

    bal_res = get_leave_balance(employee_id="EMP-1002", caller_id="EMP-1001")
    assert bal_res["status"] == "DENIED"
    assert bal_res["code"] == "RBAC_VIOLATION"


def test_hitl_confirmation_gate_p3():
    """Verify Principle P3: Write tools require user_confirmed=True."""
    res = update_contact_info(
        employee_id="EMP-1001",
        phone="+65-9999-0000",
        caller_id="EMP-1001",
        user_confirmed=False,
    )
    assert res["status"] == "CONFIRMATION_REQUIRED"

    # Now confirm
    confirmed_res = update_contact_info(
        employee_id="EMP-1001",
        phone="+65-9999-0000",
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert confirmed_res["status"] == "SUCCESS"
    assert confirmed_res["updated_fields"]["phone"] == "+65-9999-0000"


def test_hcm_guardrail_g_hcm_1_insufficient_balance():
    """Verify G-HCM-1 blocks leave requests exceeding available balance."""
    # EMP-1001 has 6.0 days childcare leave; requesting 8.0 days must fail
    res = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="childcare",
        start_date="2026-11-01",
        end_date="2026-11-10",
        days=8.0,
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-1_INSUFFICIENT_BALANCE"


def test_hcm_guardrail_g_hcm_2_date_and_notice():
    """Verify G-HCM-2 chronological check and Section 18.2 30-day advance notice for personal leave."""
    # End date before start date
    res_dates = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="annual",
        start_date="2026-11-15",
        end_date="2026-11-10",
        days=3.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res_dates["status"] == "DENIED"
    assert res_dates["code"] == "G-HCM-2_INVALID_DATE_RANGE"

    # Personal leave < 30 days from reference date 2026-09-16 without emergency
    res_notice = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="personal",
        start_date="2026-09-25",
        end_date="2026-09-26",
        days=2.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        is_emergency=False,
    )
    assert res_notice["status"] == "DENIED"
    assert res_notice["code"] == "G-HCM-2_NOTICE_PERIOD_VIOLATION"


def test_hcm_guardrail_g_hcm_3_overlapping_leave():
    """Verify G-HCM-3 blocks leave requests overlapping existing approved leaves (2026-12-24..26)."""
    res = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="annual",
        start_date="2026-12-25",
        end_date="2026-12-28",
        days=2.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res["status"] == "DENIED"
    assert res["code"] == "G-HCM-3_OVERLAPPING_LEAVE"


def test_policy_section_25_3_toil_gotchas():
    """Verify Section 25.3: TOIL is not recorded in WorkWeek AND must be used before Paid Vacation."""
    # Gotcha 1: Submitting TOIL in WorkWeek
    res_toil = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="toil",
        start_date="2026-11-02",
        end_date="2026-11-03",
        days=2.0,
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res_toil["status"] == "DENIED"
    assert res_toil["code"] == "POLICY_RULE_SECTION_25_3_TOIL_NOT_IN_WORKWEEK"

    # Gotcha 2: Submitting Annual Leave while EMP-1001 has 2.0 days of unused TOIL
    res_annual = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="annual",
        start_date="2026-11-02",
        end_date="2026-11-04",
        days=3.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        toil_cleared_by_manager=False,
    )
    assert res_annual["status"] == "DENIED"
    assert res_annual["code"] == "G-HCM-4_TOIL_MUST_BE_USED_FIRST"

    # Succeeds when toil_cleared_by_manager=True
    res_ok = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="annual",
        start_date="2026-11-02",
        end_date="2026-11-04",
        days=3.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        toil_cleared_by_manager=True,
    )
    assert res_ok["status"] == "SUCCESS"
    assert res_ok["remaining_balance"] == 11.0


def test_policy_section_19_2_sick_leave_mc():
    """Verify Section 19.2: Sick leave > 2 consecutive days requires Medical Certificate."""
    res_no_mc = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="sick",
        start_date="2026-10-12",
        end_date="2026-10-15",
        days=4.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        has_medical_certificate=False,
    )
    assert res_no_mc["status"] == "DENIED"
    assert res_no_mc["code"] == "G-HCM-5_MEDICAL_CERTIFICATE_REQUIRED"

    res_with_mc = submit_leave_request(
        employee_id="EMP-1001",
        leave_type="sick",
        start_date="2026-10-12",
        end_date="2026-10-15",
        days=4.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        has_medical_certificate=True,
    )
    assert res_with_mc["status"] == "SUCCESS"


def test_itsm_guardrail_g_itsm_1_state_machine():
    """Verify G-ITSM-1 blocks direct New -> Closed transitions and enforces valid lifecycle."""
    # INC-2002 starts in 'New' state
    res_illegal = update_ticket_status(
        ticket_id="INC-2002",
        new_status="Closed",
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res_illegal["status"] == "DENIED"
    assert res_illegal["code"] == "G-ITSM-1_INVALID_STATE_TRANSITION"

    # Valid transition: New -> In Progress -> Resolved -> Closed
    step1 = update_ticket_status("INC-2002", "In Progress", caller_id="EMP-1001", user_confirmed=True)
    assert step1["status"] == "SUCCESS"
    step2 = update_ticket_status("INC-2002", "Resolved", caller_id="EMP-1001", user_confirmed=True)
    assert step2["status"] == "SUCCESS"
    step3 = update_ticket_status("INC-2002", "Closed", caller_id="EMP-1001", user_confirmed=True)
    assert step3["status"] == "SUCCESS"


def test_itsm_guardrails_g_itsm_2_and_3():
    """Verify G-ITSM-2 (priority 1..4) and G-ITSM-3 (duplicate open ticket prevention)."""
    res_prio = create_incident(
        employee_id="EMP-1001",
        category="IT_Support",
        priority=9,
        title="Bad Priority Test",
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res_prio["status"] == "DENIED"
    assert res_prio["code"] == "G-ITSM-2_INVALID_PRIORITY"

    # Duplicate of INC-2001 ("VPN Certificate Renewal Issue" under IT_Support)
    res_dup = create_incident(
        employee_id="EMP-1001",
        category="IT_Support",
        priority=3,
        title="VPN Certificate Renewal Issue",
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res_dup["status"] == "DENIED"
    assert res_dup["code"] == "G-ITSM-3_DUPLICATE_INCIDENT"


def test_policy_section_5_4_home_office_equipment_rules():
    """Verify Section 5.4: $500 USD cap, 'Facilities' category, and Remote/Hybrid work arrangement."""
    # 1. Wrong category ('IT_Support' instead of 'Facilities')
    res_cat = create_incident(
        employee_id="EMP-1001",
        category="IT_Support",
        priority=3,
        title="Home Office Equipment Allowance - Monitor",
        amount_usd=350.0,
        is_home_office_equipment=True,
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res_cat["status"] == "DENIED"
    assert res_cat["code"] == "POLICY_RULE_SECTION_5_4_WRONG_CATEGORY_MUST_BE_FACILITIES"

    # 2. Exceeds $500 USD cap
    res_cap = create_incident(
        employee_id="EMP-1001",
        category="Facilities",
        priority=3,
        title="Home Office Equipment Allowance - Chair",
        amount_usd=650.0,
        is_home_office_equipment=True,
        caller_id="EMP-1001",
        user_confirmed=True,
    )
    assert res_cap["status"] == "DENIED"
    assert res_cap["code"] == "POLICY_RULE_SECTION_5_4_EXCEEDS_500_USD_CAP"

    # 3. On-site employee (EMP-1003) ineligible
    res_onsite = create_incident(
        employee_id="EMP-1003",
        category="Facilities",
        priority=3,
        title="Home Office Equipment Allowance",
        amount_usd=300.0,
        is_home_office_equipment=True,
        caller_id="EMP-1003",
        user_confirmed=True,
    )
    assert res_onsite["status"] == "DENIED"
    assert res_onsite["code"] == "POLICY_RULE_SECTION_5_4_INELIGIBLE_WORK_ARRANGEMENT"


def test_cross_system_saga_and_compensation_rollback():
    """Verify UC-2.x Cross-System Saga completes on success AND rolls back WorkWeek on ITSM failure."""
    # Check initial profile of EMP-1001
    initial = get_employee_profile("EMP-1001", caller_id="EMP-1001")["profile"]
    orig_address = initial["address"]
    orig_arrangement = initial["work_arrangement"]

    # 1. Test Saga Failure & Automatic Compensation Rollback (simulate_itsm_failure=True)
    saga_fail = execute_remote_work_transition_saga(
        employee_id="EMP-1001",
        new_address="999 Failed Saga Street, Singapore",
        new_work_arrangement="Remote",
        equipment_amount_usd=400.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        simulate_itsm_failure=True,
    )
    assert saga_fail["status"] == "SAGA_COMPENSATED_ROLLED_BACK"
    assert saga_fail["compensation_action"]["status"] == "ROLLED_BACK_SUCCESSFULLY"

    # Verify WorkWeek profile was restored to exact original address & arrangement
    after_rollback = get_employee_profile("EMP-1001", caller_id="EMP-1001")["profile"]
    assert after_rollback["address"] == orig_address
    assert after_rollback["work_arrangement"] == orig_arrangement

    # 2. Test Successful Saga Execution
    saga_ok = execute_remote_work_transition_saga(
        employee_id="EMP-1001",
        new_address="77 Success Remote Ave, Singapore 123456",
        new_work_arrangement="Remote",
        equipment_title="Home Office Ergonomic Desk",
        equipment_amount_usd=420.0,
        caller_id="EMP-1001",
        user_confirmed=True,
        simulate_itsm_failure=False,
    )
    assert saga_ok["status"] == "SAGA_COMPLETED"
    after_success = get_employee_profile("EMP-1001", caller_id="EMP-1001")["profile"]
    assert after_success["address"] == "77 Success Remote Ave, Singapore 123456"
    assert after_success["work_arrangement"] == "Remote"
