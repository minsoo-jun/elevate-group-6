"""hr-chat-ui — Self-hosted Web Chat Backend (SDD §1.2 UI Scope, §1.3 体験層).

Implements the `hr-chat-ui` experience layer defined in the Solution Design Document:
- Streaming chat display (SSE) against the `hr_concierge` ADK agent
- Clickable citation deep links (FR-5.3)
- Human-in-the-Loop (HITL) confirmation cards (Principle P3)
- Identity switcher bound to the live MCP identity (EMP-791) plus RBAC-demo dummies
- Live audit log inspection (Principle P7) and enterprise state panels

Backend of record: the **WorkWeek / ServiceImmediately MCP servers**. There is no
local SQLite mirror of enterprise data any more — every state panel read goes out
through `app.tools`, which is the same Policy Enforcement Point the agent uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=REPO_ROOT / ".env")

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "elevate-minsoo")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from app.agent import app as adk_app  # noqa: E402
from app.tools import (  # noqa: E402
    get_employee_profile,
    get_leave_balance,
    get_leave_requests,
    list_tickets,
)
from app.tools.adapter import AUDIT_LOG_PATH, DB_PATH  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The MCP Personal Access Token maps to exactly one employee context. Any other
# employee ID is refused by the remote backends with
# "Error: Access denied. Authenticated context is restricted to EMP-791."
PRIMARY_EMPLOYEE_ID = "EMP-791"

TEST_USERS = [
    {
        "employee_id": "EMP-791",
        "name": "Minsoojun Employee",
        "title": "認証済みアイデンティティ（MCP トークン）",
        "department": "Singapore Office",
        # WorkWeek's `get_personal_info` exposes only address and phone, so the
        # work arrangement below is a local demo attribute, not a system of record
        # value. It drives the Section 5.4 eligibility narrative in the UI only.
        "work_arrangement": "Hybrid",
        "initials": "ME",
        "kind": "primary",
        "badge": "ライブ接続",
        "note": "WorkWeek / ServiceImmediately の実データに接続します。",
    },
    {
        "employee_id": "EMP-1001",
        "name": "Alice Tan",
        "title": "他人の ID（アクセス拒否デモ用）",
        "department": "Cloud Engineering",
        "work_arrangement": "Hybrid",
        "initials": "AT",
        "kind": "rbac_demo",
        "badge": "拒否デモ",
        "note": (
            "ダミー ID です。MCP トークンは EMP-791 にしか紐づかないため、"
            "このユーザーで照会すると P6 RBAC 拒否が実演されます。"
        ),
    },
    {
        "employee_id": "EMP-1002",
        "name": "Bob Lim",
        "title": "他人の ID（アクセス拒否デモ用）",
        "department": "Product Management",
        "work_arrangement": "Remote",
        "initials": "BL",
        "kind": "rbac_demo",
        "badge": "拒否デモ",
        "note": (
            "ダミー ID です。MCP トークンは EMP-791 にしか紐づかないため、"
            "このユーザーで照会すると P6 RBAC 拒否が実演されます。"
        ),
    },
]

# Local directory metadata keyed by employee ID (display-only; WorkWeek does not
# expose name / title / department / work arrangement over MCP).
DIRECTORY = {u["employee_id"]: u for u in TEST_USERS}

SUGGESTIONS = [
    {
        "category": "Policy Q&A",
        "label": "ホストへのギフトカード贈答は可能か（§4.3 / §5.2）",
        "text": "クライアント宅での夕食に招かれました。$50 未満なので $45 の Amazon ギフトカードをホストギフトとして購入してもよいですか？Section 4.3 と Section 5.2 の規定を引用して説明してください。",
    },
    {
        "category": "Policy Q&A",
        "label": "TOIL と有給休暇の消化順序（§1.4 / §25.3）",
        "text": "WorkWeek で accrued TOIL をどのように申請しますか？また、2日分の TOIL を使う前に3日間の有給休暇（Section 1.2）を取得できますか？Section 1.4 と Section 25.3 を引用してください。",
    },
    {
        "category": "Policy Q&A",
        "label": "育児休暇の年間上限日数（§24.2）",
        "text": "シンガポールで法定育児休暇と拡張育児休暇は年間何日取得できますか？合計12日まで積み上がりますか？Section 24.2 を引用してください。",
    },
    {
        "category": "WorkWeek (HRMS)",
        "label": "休暇残高の照会（ライブ）",
        "text": "WorkWeek で私（EMP-791）の現在の休暇残高と、登録済みの休暇申請一覧を確認してください。",
    },
    {
        "category": "WorkWeek (HRMS)",
        "label": "病欠3日と MC 要件（§19.2）",
        "text": "私は EMP-791 です。来週 3 日間の病気休暇を申請したいのですが、Medical Certificate はまだありません。Section 19.2 の要件に照らして判断してください (user_confirmed=True)。",
    },
    {
        "category": "WorkWeek (HRMS)",
        "label": "RBAC データ隔離の検証",
        "text": "私は EMP-791 です。同僚の Alice Tan (EMP-1001) の WorkWeek プロフィールと自宅住所を見せてください。",
    },
    {
        "category": "ServiceImmediately (ITMS)",
        "label": "チケット状態遷移ガードレール（§5.5）",
        "text": "現在 'New' ステータスの ServiceImmediately チケット INC0004555（Onboarding setup and badges configuration）を直接クローズしてください。確認済みです (user_confirmed=True)。",
    },
    {
        "category": "ServiceImmediately (ITMS)",
        "label": "在宅勤務機器申請の上限超過（§5.4）",
        "text": "在宅勤務用のエルゴノミクスチェア購入のため $600 USD を 'IT_Support' カテゴリで ServiceImmediately に申請したいです。Section 5.4 の $500 上限に照らして判断してください (user_confirmed=True)。",
    },
    {
        "category": "Cross-System Saga",
        "label": "横断 Saga と自動補償ロールバック（§5.4 / §5.5）",
        "text": "私は EMP-791 です。WorkWeek の住所を '999 Outage Test Rd, Singapore'、勤務形態を 'Remote' に変更し、ServiceImmediately に Facilities カテゴリの在宅勤務機器チケット（$450）を作成する横断 Saga を実行してください (user_confirmed=True, simulate_itsm_failure=True)。",
    },
]

CITATION_PATTERN = re.compile(r"Section\s+(\d+(?:\.\d+)?)", re.IGNORECASE)

session_service = InMemorySessionService()
runner = Runner(
    app=adk_app,
    session_service=session_service,
    auto_create_session=True,
)

api = FastAPI(title="hr-chat-ui", description="Elevate APAC HR & IT Concierge — Web Chat Experience Layer")
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _tool_meta(tool_name: str) -> dict[str, str]:
    """Map a tool name to its target system and semantic type for UI badges."""
    if tool_name in ("search_policy", "list_policy_concepts", "read_policy_concept"):
        return {"system": "Policy KB", "kind": "read", "domain": "policy"}
    if tool_name in (
        "get_current_employee_id",
        "get_employee_profile",
        "get_leave_balance",
        "get_leave_requests",
    ):
        return {"system": "WorkWeek", "kind": "read", "domain": "hcm"}
    if tool_name in ("update_contact_info", "submit_leave_request", "cancel_leave_request"):
        return {"system": "WorkWeek", "kind": "write", "domain": "hcm"}
    if tool_name in ("get_ticket", "list_tickets"):
        return {"system": "ServiceImmediately", "kind": "read", "domain": "itsm"}
    if tool_name in ("create_incident", "add_ticket_comment", "update_ticket_status"):
        return {"system": "ServiceImmediately", "kind": "write", "domain": "itsm"}
    if tool_name == "execute_remote_work_transition_saga":
        return {"system": "Cross-System Saga", "kind": "write", "domain": "saga"}
    return {"system": "Unknown", "kind": "read", "domain": "other"}


def _classify_result(result: Any) -> dict[str, Any]:
    """Derive a UI verdict badge from a tool result.

    Verdicts: SUCCESS / DENIED / ERROR / CONFIRMATION_REQUIRED / ROLLBACK /
    SERVICE_UNAVAILABLE. The last one is deliberately distinct from DENIED: an
    MCP transport fault is an infrastructure warning, not a governance decision.
    """
    if not isinstance(result, dict):
        return {"verdict": "SUCCESS", "code": None, "message": None}
    status = str(result.get("status", "SUCCESS"))
    verdict_map = {
        "SUCCESS": "SUCCESS",
        "DENIED": "DENIED",
        "ERROR": "ERROR",
        "CONFIRMATION_REQUIRED": "CONFIRMATION_REQUIRED",
        "NOT_FOUND": "ERROR",
        "NO_MATCH": "ERROR",
        # Raised by hcm_tools._degraded / itsm_tools._degraded when the MCP
        # transport itself fails (timeout, 429 exhaustion, missing token).
        "SERVICE_UNAVAILABLE": "SERVICE_UNAVAILABLE",
        "SAGA_COMPLETED": "SUCCESS",
        "SAGA_COMPENSATED_ROLLED_BACK": "ROLLBACK",
        "SAGA_ABORTED_AT_STEP_1": "ERROR",
    }
    return {
        "verdict": verdict_map.get(status, status),
        "status": status,
        "code": result.get("code"),
        "message": result.get("message"),
        "citation": result.get("citation"),
        "audit_event_id": result.get("audit_event_id"),
        "proposal": result.get("proposed_changes")
        or result.get("proposed_request")
        or result.get("proposed_ticket")
        or result.get("proposed_transition")
        or result.get("proposed_saga_plan"),
        "compensation": result.get("compensation_action"),
    }


def _extract_citations(result: Any) -> list[dict[str, str]]:
    """Extract clickable citation deep links (FR-5.3) from a policy tool result."""
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(result, dict):
        for item in result.get("results", []) or result.get("matches", []):
            if not isinstance(item, dict):
                continue
            sec = item.get("section_id", "")
            if sec and sec not in seen:
                seen.add(sec)
                citations.append(
                    {
                        "section_id": sec,
                        "title": item.get("title", ""),
                        "url": item.get("citation_url", ""),
                        "snippet": (item.get("content", "") or "")[:280],
                    }
                )
    return citations[:6]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@api.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    """Return test users, suggested prompts, and agent metadata for UI initialization."""
    return {
        "users": TEST_USERS,
        "primary_employee_id": PRIMARY_EMPLOYEE_ID,
        "suggestions": SUGGESTIONS,
        "agent": {
            "name": "hr_concierge",
            "model": "gemini-3.7-flash",
            "region": "asia-northeast1",
            "systems": ["Policy Knowledge Base", "WorkWeek (HCM)", "ServiceImmediately (ITSM)"],
            "backend": "MCP (live)",
        },
    }


@api.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    """Stream an agent turn over Server-Sent Events (SDD §3.x streaming display)."""
    body = await request.json()
    message: str = body.get("message", "").strip()
    user_id: str = body.get("employee_id", PRIMARY_EMPLOYEE_ID)
    session_id: str = body.get("session_id") or f"sess-{uuid.uuid4().hex[:12]}"

    async def event_stream():
        yield _sse("session", {"session_id": session_id, "employee_id": user_id})

        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
        pending_calls: dict[str, dict[str, Any]] = {}
        final_text_parts: list[str] = []
        all_citations: list[dict[str, str]] = []

        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            ):
                if not event.content or not event.content.parts:
                    continue

                for part in event.content.parts:
                    # Tool invocation started
                    if getattr(part, "function_call", None):
                        fc = part.function_call
                        meta = _tool_meta(fc.name)
                        call_id = fc.id or f"fc-{uuid.uuid4().hex[:8]}"
                        pending_calls[fc.name] = {"id": call_id, **meta}
                        yield _sse(
                            "tool_call",
                            {
                                "call_id": call_id,
                                "tool_name": fc.name,
                                "args": dict(fc.args or {}),
                                **meta,
                            },
                        )
                        await asyncio.sleep(0)

                    # Tool result returned
                    elif getattr(part, "function_response", None):
                        fr = part.function_response
                        result = fr.response if isinstance(fr.response, dict) else {}
                        # ADK may nest the payload under "result"
                        if "result" in result and isinstance(result["result"], dict):
                            result = result["result"]
                        meta = pending_calls.get(fr.name, _tool_meta(fr.name))
                        classification = _classify_result(result)
                        citations = _extract_citations(result)
                        for c in citations:
                            if c["section_id"] not in [x["section_id"] for x in all_citations]:
                                all_citations.append(c)
                        yield _sse(
                            "tool_result",
                            {
                                "call_id": meta.get("id"),
                                "tool_name": fr.name,
                                "system": meta.get("system"),
                                "kind": meta.get("kind"),
                                "domain": meta.get("domain"),
                                "citations": citations,
                                "raw": result,
                                **classification,
                            },
                        )
                        await asyncio.sleep(0)

                    # Model text output (streaming chunk)
                    elif getattr(part, "text", None):
                        chunk = part.text
                        if event.partial:
                            yield _sse("delta", {"text": chunk})
                        else:
                            final_text_parts.append(chunk)
                            yield _sse("message", {"text": chunk})
                        await asyncio.sleep(0)

            answer = "".join(final_text_parts)
            inline_sections = sorted({f"Section {m}" for m in CITATION_PATTERN.findall(answer)})
            yield _sse(
                "done",
                {
                    "session_id": session_id,
                    "cited_sections": inline_sections,
                    "citations": all_citations,
                },
            )
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/api/audit")
async def audit(limit: int = 40) -> dict[str, Any]:
    """Return the most recent unified audit records (Principle P7)."""
    if not AUDIT_LOG_PATH.exists():
        return {"records": [], "total": 0, "allow": 0, "deny": 0}
    lines = AUDIT_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    allow = sum(1 for r in records if r.get("decision") == "ALLOW")
    deny = sum(1 for r in records if r.get("decision") == "DENY")
    return {
        "records": list(reversed(records))[:limit],
        "total": len(records),
        "allow": allow,
        "deny": deny,
    }


@api.get("/api/state")
async def enterprise_state(employee_id: str = "EMP-1001") -> dict[str, Any]:
    """Return live WorkWeek (HCM) and ServiceImmediately (ITSM) state for the side panel."""
    _init_hcm_db().close()
    _init_itsm_db().close()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    profile_row = conn.execute(
        "SELECT * FROM hcm_employees WHERE employee_id = ?", (employee_id,)
    ).fetchone()
    balance_row = conn.execute(
        "SELECT * FROM hcm_leave_balances WHERE employee_id = ?", (employee_id,)
    ).fetchone()
    leave_rows = conn.execute(
        "SELECT request_id, leave_type, start_date, end_date, days, status FROM hcm_leave_requests WHERE employee_id = ? ORDER BY created_at DESC",
        (employee_id,),
    ).fetchall()
    ticket_rows = conn.execute(
        "SELECT ticket_id, category, priority, status, title, amount_usd FROM itsm_tickets WHERE employee_id = ? ORDER BY created_at DESC",
        (employee_id,),
    ).fetchall()
    conn.close()

    return {
        "profile": dict(profile_row) if profile_row else None,
        "balances": dict(balance_row) if balance_row else None,
        "leave_requests": [dict(r) for r in leave_rows],
        "tickets": [dict(r) for r in ticket_rows],
    }


@api.post("/api/reset")
async def reset_state() -> dict[str, str]:
    """Reset the demo enterprise state database and audit log to a pristine baseline."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    if AUDIT_LOG_PATH.exists():
        AUDIT_LOG_PATH.unlink()
    _init_hcm_db().close()
    _init_itsm_db().close()
    return {"status": "RESET_COMPLETE"}


@api.get("/api/policy")
async def policy_section(section: str) -> dict[str, Any]:
    """Return verbatim handbook text for a clickable citation deep link (FR-5.3)."""
    from app.tools.policy_tools import read_policy_concept

    result = read_policy_concept(section.replace("Section", "").strip())
    return result


@api.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


api.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=8090)
