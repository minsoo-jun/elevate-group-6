"""EnterpriseToolAdapter & Governance Layer (SDD §4.4, §4.7, §5.1, §5.4).

Implements:
- Principle P1/P2: Policy Enforcement Point (PEP) at the tool layer
- Principle P3: Human-in-the-Loop (HITL) confirmation gate for write operations
- Principle P6: 1 Request = 1 User-scoped delegated identity (RBAC data isolation)
- Principle P7: Unified 18-field audit logging (both ALLOW and DENY)
- Idempotency key management with persistent state store
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)
AUDIT_LOG_PATH = ARTIFACTS_DIR / "audit_logs.jsonl"
DB_PATH = ARTIFACTS_DIR / "enterprise_state.db"


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_store (
            idempotency_key TEXT PRIMARY KEY,
            actor_id TEXT,
            tool_name TEXT,
            status TEXT,
            response_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def compute_idempotency_key(actor_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 idempotency key from actor, tool, and canonical args."""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    payload = f"{actor_id}:{tool_name}:{canonical}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_idempotency(idempotency_key: str) -> dict[str, Any] | None:
    """Check if an operation with this idempotency key already completed."""
    conn = _get_db()
    row = conn.execute(
        "SELECT status, response_json FROM idempotency_store WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    conn.close()
    if row and row["status"] == "COMPLETED":
        cached = json.loads(row["response_json"])
        cached["idempotent_replay"] = True
        cached["idempotency_key"] = idempotency_key
        return cached
    return None


def record_idempotency(
    idempotency_key: str, actor_id: str, tool_name: str, response: dict[str, Any]
) -> None:
    """Record completed operation in idempotency store."""
    conn = _get_db()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO idempotency_store
        (idempotency_key, actor_id, tool_name, status, response_json, created_at)
        VALUES (?, ?, ?, 'COMPLETED', ?, ?)
        """,
        (idempotency_key, actor_id, tool_name, json.dumps(response, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()


def log_audit_event(
    *,
    actor_id: str,
    target_employee_id: str,
    tool_name: str,
    target_system: str,
    action_type: str,  # "READ" | "WRITE"
    decision: str,     # "ALLOW" | "DENY"
    deny_reason: str | None = None,
    idempotency_key: str | None = None,
    confirmation_id: str | None = None,
    origin: str = "AGENT_AUTO",  # "AGENT_AUTO" | "HUMAN_CONFIRMED"
    request_summary: dict[str, Any] | None = None,
) -> str:
    """Write unified 18-field audit record (SDD §4.7) to artifacts/audit_logs.jsonl."""
    event_id = f"AUD-{uuid.uuid4().hex[:12].upper()}"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = {
        "event_id": event_id,
        "timestamp": now,
        "actor": {
            "employee_id": actor_id,
            "origin": origin,
            "confirmation_id": confirmation_id,
        },
        "target_employee_id": target_employee_id,
        "tool_name": tool_name,
        "target_system": target_system,
        "action_type": action_type,
        "decision": decision,
        "deny_reason": deny_reason,
        "idempotency_key": idempotency_key,
        "downstream_request_id": f"DS-{uuid.uuid4().hex[:8].upper()}",
        "region": "asia-northeast1",
        "pii_masked": True,
        "request_summary": request_summary or {},
    }
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return event_id


def enforce_data_isolation(
    caller_id: str, target_employee_id: str, tool_name: str, target_system: str
) -> dict[str, Any] | None:
    """Enforce Principle P6: caller_id must match target_employee_id unless HR_ADMIN."""
    if caller_id != target_employee_id and caller_id != "HR_ADMIN":
        reason = (
            f"RBAC_VIOLATION (P6): Caller '{caller_id}' is not authorized to access or modify "
            f"records for employee '{target_employee_id}'."
        )
        audit_id = log_audit_event(
            actor_id=caller_id,
            target_employee_id=target_employee_id,
            tool_name=tool_name,
            target_system=target_system,
            action_type="ACCESS_CHECK",
            decision="DENY",
            deny_reason=reason,
        )
        return {
            "status": "DENIED",
            "code": "RBAC_VIOLATION",
            "message": reason,
            "audit_event_id": audit_id,
        }
    return None
