"""HR Concierge Agent Tool Suite (SDD §3.1, §5.2).

Three tool families:
  * policy_tools — local, grounded retrieval over the Employee Handbook
  * hcm_tools    — WorkWeek (HRMS) via MCP
  * itsm_tools   — ServiceImmediately (ITSM) via MCP
  * saga_tools   — cross-system orchestration with compensating transactions
"""
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
from app.tools.policy_tools import (
    list_policy_concepts,
    read_policy_concept,
    search_policy,
)
from app.tools.saga_tools import execute_remote_work_transition_saga

__all__ = [
    # Policy Q&A (local, grounded)
    "search_policy",
    "list_policy_concepts",
    "read_policy_concept",
    # WorkWeek / HRMS (MCP)
    "get_current_employee_id",
    "get_employee_profile",
    "get_leave_balance",
    "get_leave_requests",
    "update_contact_info",
    "submit_leave_request",
    "cancel_leave_request",
    # ServiceImmediately / ITSM (MCP)
    "list_tickets",
    "get_ticket",
    "create_incident",
    "add_ticket_comment",
    "update_ticket_status",
    # Cross-system orchestration
    "execute_remote_work_transition_saga",
]
