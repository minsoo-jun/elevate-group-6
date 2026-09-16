"""End-to-end smoke test of the MCP-backed tool layer against the live servers."""
import datetime
import json
import random
import sys
import time

sys.path.insert(0, "/usr/local/google/home/minsoojun/work/elevate-group6")

from app.tools import (  # noqa: E402
    add_ticket_comment,
    cancel_leave_request,
    create_incident,
    get_current_employee_id,
    get_employee_profile,
    get_leave_balance,
    get_leave_requests,
    get_ticket,
    list_tickets,
    submit_leave_request,
    update_contact_info,
    update_ticket_status,
)

EMP = "EMP-791"
PASS, FAIL = [], []

# Idempotency (P2) deliberately replays an identical write instead of
# re-executing it, so every run needs a payload it has never used before --
# otherwise the "write" silently returns the previous run's cached result and
# nothing actually changes downstream.
RUN_ID = f"{int(time.time())}-{random.randint(1000, 9999)}"
_BASE = datetime.date(2027, 1, 4) + datetime.timedelta(days=random.randint(0, 200))
BOOK_START = _BASE.isoformat()
BOOK_END = (_BASE + datetime.timedelta(days=1)).isoformat()
print(f"run id: {RUN_ID}")
print(f"booking window for this run: {BOOK_START} .. {BOOK_END}")


def check(name, result, expect_status, note=""):
    got = result.get("status")
    ok = got == expect_status
    (PASS if ok else FAIL).append(name)
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}: expected={expect_status} got={got} {note}")
    if not ok or "-v" in sys.argv:
        print("       ", json.dumps(result, ensure_ascii=False, default=str)[:400])
    return result


print("=" * 78, "\nREAD PATH\n", "=" * 78)
r = check("get_current_employee_id", get_current_employee_id(caller_id=EMP), "SUCCESS")
print("        employee_id =", r.get("employee_id"))

r = check("get_employee_profile", get_employee_profile(EMP, EMP), "SUCCESS")
print("        profile =", r.get("profile"))

r = check("get_leave_balance", get_leave_balance(EMP, EMP), "SUCCESS")
print("        balances =", r.get("balances"))

r = check("get_leave_requests", get_leave_requests(EMP, EMP), "SUCCESS")
print("        requests =", len(r.get("requests", [])))

r = check("list_tickets", list_tickets(EMP, EMP), "SUCCESS")
print("        tickets =", [(t.get("ticket_id"), t.get("status")) for t in r.get("tickets", [])])

print("\n" + "=" * 78, "\nGUARDRAILS (must be blocked BEFORE reaching MCP)\n", "=" * 78)
check("P6 RBAC cross-employee read", get_leave_balance("EMP-1001", EMP), "DENIED",
      "caller != target")
check("P3 HITL gate on leave", submit_leave_request(
    employee_id=EMP, leave_type="vacation", start_date="2026-12-01",
    end_date="2026-12-02", days=2, caller_id=EMP), "CONFIRMATION_REQUIRED")
check("G-HCM-1 insufficient balance", submit_leave_request(
    employee_id=EMP, leave_type="vacation", start_date="2026-12-01",
    end_date="2026-12-31", days=99, caller_id=EMP, user_confirmed=True), "DENIED")
check("G-HCM-3 overlap (backend cannot detect this)", submit_leave_request(
    employee_id=EMP, leave_type="vacation", start_date="2026-06-02",
    end_date="2026-06-03", days=2, caller_id=EMP, user_confirmed=True), "DENIED")
check("G-ITSM-2 critical w/o outage", create_incident(
    employee_id=EMP, category="Hardware", short_description="Need a new mouse",
    priority=1, caller_id=EMP, user_confirmed=True), "DENIED")
check("invalid category", create_incident(
    employee_id=EMP, category="Nonsense", short_description="x",
    priority=3, caller_id=EMP, user_confirmed=True), "DENIED")
check("Section 5.4 over $500 cap", create_incident(
    employee_id=EMP, category="Facilities",
    short_description="Home office equipment allowance: standing desk",
    priority=3, amount_usd=900.0, is_home_office_equipment=True,
    work_arrangement="Remote", caller_id=EMP, user_confirmed=True), "DENIED")
check("G-ITSM-1 New->Closed (stricter than backend)", update_ticket_status(
    ticket_id="INC0004555", new_status="Closed", caller_id=EMP,
    user_confirmed=True), "DENIED")
check("G-HCM-6 address too short", update_contact_info(
    employee_id=EMP, address="abc", caller_id=EMP, user_confirmed=True), "DENIED")
check("G-HCM-7 bad phone", update_contact_info(
    employee_id=EMP, phone="nope!!", caller_id=EMP, user_confirmed=True), "DENIED")

print("\n" + "=" * 78, "\nWRITE PATH (real mutations, cleaned up after)\n", "=" * 78)
r = check("submit_leave_request (confirmed)", submit_leave_request(
    employee_id=EMP, leave_type="vacation", start_date=BOOK_START,
    end_date=BOOK_END, days=2, caller_id=EMP, user_confirmed=True), "SUCCESS")

after = get_leave_balance(EMP, EMP)
print("        balances after booking =", after.get("balances"))

booked = [q for q in get_leave_requests(EMP, EMP).get("requests", [])
          if q.get("start_date") == BOOK_START]
for q in booked:
    check(f"cancel_leave_request({q['request_id']})", cancel_leave_request(
        employee_id=EMP, request_id=q["request_id"], caller_id=EMP,
        user_confirmed=True), "SUCCESS")

print("        balances restored =", get_leave_balance(EMP, EMP).get("balances"))

r = check("create_incident (confirmed)", create_incident(
    employee_id=EMP, category="Hardware",
    short_description=f"Smoke test {RUN_ID}: laptop docking station replacement",
    priority=3, caller_id=EMP, user_confirmed=True), "SUCCESS")
tid = r.get("ticket_id")
print("        ticket_id =", tid)

if tid:
    check("get_ticket", get_ticket(tid, EMP), "SUCCESS")
    check("add_ticket_comment", add_ticket_comment(
        ticket_id=tid, comment="Smoke test comment.", caller_id=EMP), "SUCCESS")
    check("update_ticket_status New->In Progress", update_ticket_status(
        ticket_id=tid, new_status="In Progress", caller_id=EMP,
        user_confirmed=True), "SUCCESS")
    check("CLEANUP In Progress->Closed", update_ticket_status(
        ticket_id=tid, new_status="Closed", resolution_notes="Smoke test cleanup.",
        caller_id=EMP, user_confirmed=True), "SUCCESS")

print("\n" + "=" * 78)
print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
