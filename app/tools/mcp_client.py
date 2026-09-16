"""MCP Client Layer — Unified Mock Enterprise Services (SDD §5.1 EnterpriseToolAdapter).

Connects to the two mounted stateless Streamable HTTP MCP servers:
  - WorkWeek             : {BASE}/work-week/mcp/
  - ServiceImmediately   : {BASE}/service-immediately/mcp/

Authentication uses the custom `X-MCP-Token` header (a standard `Authorization`
header is intercepted and rejected by Google Frontend / IAP).

This module exposes:
  - `mcp_call(server, tool_name, args)`  : invoke a single MCP tool (async)
  - `mcp_call_sync(...)`                 : blocking wrapper usable from ADK sync tools
  - `list_mcp_tools(server)`             : discovery helper for connection checks
  - `mcp_enabled()`                      : whether a token is configured
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(dotenv_path=REPO_ROOT / ".env")

MCP_BASE_URL = os.getenv(
    "MCP_BASE_URL", "https://mock-saas.aishprabhat.demo.altostrat.com"
).rstrip("/")
MCP_TOKEN = os.getenv("MCP_TOKEN", "").strip()
MCP_TIMEOUT_SECONDS = float(os.getenv("MCP_TIMEOUT_SECONDS", "45"))

SERVER_PATHS = {
    "workweek": "/work-week/mcp/",
    "serviceimmediately": "/service-immediately/mcp/",
}

# The mock host throttles bursts of tool calls with HTTP 429, which a single
# agent turn can easily trigger (a guarded write makes 2-3 reads first).
# We both pace outbound calls and retry with backoff.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
MCP_MAX_ATTEMPTS = int(os.getenv("MCP_MAX_ATTEMPTS", "5"))
MCP_BACKOFF_BASE_SECONDS = float(os.getenv("MCP_BACKOFF_BASE_SECONDS", "1.5"))
# Minimum wall-clock gap between two outbound calls to the same server.
MCP_MIN_CALL_INTERVAL_SECONDS = float(
    os.getenv("MCP_MIN_CALL_INTERVAL_SECONDS", "0.35")
)

_PACE_LOCK = threading.Lock()
_LAST_CALL_AT: dict[str, float] = {}


def _pace(server: str) -> None:
    """Block briefly so we never exceed the host's burst tolerance.

    Uses a process-wide record of the last call per server. Cheap insurance:
    a 429 costs us seconds of backoff, whereas pacing costs milliseconds.
    """
    if MCP_MIN_CALL_INTERVAL_SECONDS <= 0:
        return
    with _PACE_LOCK:
        now = time.monotonic()
        earliest = _LAST_CALL_AT.get(server, 0.0) + MCP_MIN_CALL_INTERVAL_SECONDS
        wait = earliest - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _LAST_CALL_AT[server] = now


class McpNotConfigured(RuntimeError):
    """Raised when no MCP_TOKEN is available."""


def mcp_enabled() -> bool:
    """Return True when an MCP Personal Access Token is configured."""
    return bool(MCP_TOKEN)


def server_url(server: str) -> str:
    key = server.strip().lower().replace("_", "").replace("-", "")
    if key not in SERVER_PATHS:
        raise ValueError(
            f"Unknown MCP server '{server}'. Expected one of {list(SERVER_PATHS)}."
        )
    return f"{MCP_BASE_URL}{SERVER_PATHS[key]}"


def _headers() -> dict[str, str]:
    if not MCP_TOKEN:
        raise McpNotConfigured(
            "MCP_TOKEN is not set. Add `MCP_TOKEN=mcp_...` to .env "
            "(see env.md) to enable WorkWeek / ServiceImmediately MCP connectivity."
        )
    return {"X-MCP-Token": MCP_TOKEN}


# --------------------------------------------------------------------------
# Response contract (verified by live probe, 2026-09-16)
#
# EVERY tool on both servers returns `structuredContent == {"result": "<str>"}`
# and `isError is False` — even for validation failures and access denials.
# That inner string is one of three things:
#
#   1. a JSON document encoded as a string  (get_leave_requests, list_tickets,
#      create_ticket)                        -> json.loads it
#   2. a human-readable report               (get_employee_balances,
#      get_personal_info)                    -> regex-parsed by the tool modules
#   3. a status sentence, where failure is signalled only by a text prefix
#      ("Error:", "Denied:") or a "... not found." suffix
#
# Because `isError` is useless, rejection detection is purely lexical.
# --------------------------------------------------------------------------

REJECTION_PREFIXES = ("error:", "denied:", "access denied")
REJECTION_MARKERS = ("not found.", "access denied.")

SUCCESS_PREFIXES = ("success:", "approved:")


def _raw_text(result: Any) -> str | None:
    """Extract the inner `result` string from an MCP CallToolResult."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and "result" in structured:
        value = structured["result"]
        return value if isinstance(value, str) else json.dumps(value)

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return None


def is_rejection(text: str | None) -> bool:
    """True when the server refused the call.

    The servers never set `isError`, so a refusal is only detectable from the
    message text. Both an `Error:` (validation / missing entity) and a
    `Denied:` (business rule) prefix count as a rejection.
    """
    if not text:
        return False
    lowered = text.strip().lower()
    if lowered.startswith(SUCCESS_PREFIXES):
        return False
    return lowered.startswith(REJECTION_PREFIXES) or any(
        marker in lowered for marker in REJECTION_MARKERS
    )


def _unwrap(result: Any) -> Any:
    """Normalize an MCP CallToolResult into a plain Python object.

    Returns the parsed JSON document when the inner string holds one,
    otherwise the string itself.
    """
    text = _raw_text(result)
    if text is None:
        return None

    stripped = text.strip()
    if stripped[:1] in ("[", "{"):
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            pass
    return text


async def _mcp_call_once(
    server: str, tool_name: str, args: dict[str, Any] | None
) -> dict[str, Any]:
    """Single attempt. Raises on transport failure; see `mcp_call` for retries."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = server_url(server)
    _pace(server)
    async with streamablehttp_client(url, headers=_headers()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args or {})

            payload = _unwrap(result)
            text = payload if isinstance(payload, str) else None

            if getattr(result, "isError", False):
                return {
                    "status": "MCP_ERROR",
                    "code": "MCP_TOOL_ERROR",
                    "tool": tool_name,
                    "server": server,
                    "message": str(payload),
                }

            return {
                "status": "REJECTED" if is_rejection(text) else "SUCCESS",
                "result": payload,
                "text": text,
                "server": server,
                "tool": tool_name,
            }


def _transient_status(exc: BaseException) -> int | None:
    """Return the HTTP status if `exc` (or anything nested in it) is retryable.

    The mock host throttles bursts with 429, and the failure arrives wrapped in
    an anyio ExceptionGroup, so we have to walk the tree to find it.
    """
    seen: list[BaseException] = [exc]
    while seen:
        current = seen.pop()
        inner = getattr(current, "exceptions", None)
        if inner:
            seen.extend(inner)
        for nested in (current.__cause__, current.__context__):
            if nested is not None and nested not in seen:
                seen.append(nested)

        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if status in RETRYABLE_STATUSES:
            return status
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Honour a `Retry-After` header when the server sends one."""
    seen: list[BaseException] = [exc]
    while seen:
        current = seen.pop()
        inner = getattr(current, "exceptions", None)
        if inner:
            seen.extend(inner)
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            raw = headers.get("retry-after")
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    return None
    return None


async def mcp_call(
    server: str, tool_name: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Invoke a tool on the given MCP server over stateless Streamable HTTP.

    Retries transient HTTP failures (429 throttling, 502/503/504) with
    exponential backoff. Business rejections are never retried — they are a
    deterministic answer, not a fault.

    Always returns an envelope:

        {"status": "SUCCESS" | "REJECTED" | "MCP_ERROR",
         "result": <parsed JSON document, or the raw message string>,
         "text":   <the raw message string, or None when JSON was returned>,
         "server": ..., "tool": ...}

    `REJECTED` means the remote system refused the call for a validation or
    business-rule reason. That is a normal, expected outcome — not a fault —
    so callers should surface the message rather than degrade.
    """
    last_exc: BaseException | None = None

    for attempt in range(MCP_MAX_ATTEMPTS):
        try:
            return await _mcp_call_once(server, tool_name, args)
        except Exception as exc:
            status = _transient_status(exc)
            if status is None or attempt == MCP_MAX_ATTEMPTS - 1:
                raise
            last_exc = exc
            delay = _retry_after_seconds(exc) or MCP_BACKOFF_BASE_SECONDS * (2**attempt)
            await asyncio.sleep(delay)

    # Unreachable: the final attempt either returns or re-raises.
    raise last_exc if last_exc else RuntimeError("mcp_call exhausted retries")


async def list_mcp_tools(server: str) -> list[dict[str, str]]:
    """List tool names and descriptions exposed by the given MCP server."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = server_url(server)
    async with streamablehttp_client(url, headers=_headers()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": (t.description or "").strip().splitlines()[0]
                    if t.description
                    else "",
                }
                for t in resp.tools
            ]


def _run_coroutine_blocking(coro) -> Any:
    """Run an async coroutine from sync code, safe inside a running event loop.

    ADK function tools are invoked synchronously from within an active asyncio
    loop, so `asyncio.run` would raise. We offload to a dedicated thread with
    its own loop when a loop is already running.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:
            box["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=MCP_TIMEOUT_SECONDS)

    if thread.is_alive():
        raise TimeoutError(f"MCP call exceeded {MCP_TIMEOUT_SECONDS}s timeout.")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _describe_exception(exc: BaseException, _depth: int = 0) -> str:
    """Render an exception, flattening anyio ExceptionGroups.

    The MCP Streamable HTTP client runs inside an anyio TaskGroup, so any
    transport failure surfaces as an ExceptionGroup whose ``str()`` is merely
    "unhandled errors in a TaskGroup (1 sub-exception)" — useless in an audit
    log. Recurse into the group and report the leaf causes instead.
    """
    inner = getattr(exc, "exceptions", None)
    if inner and _depth < 4:
        causes = "; ".join(_describe_exception(e, _depth + 1) for e in inner)
        return f"{type(exc).__name__}[{causes}]"

    text = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    cause = exc.__cause__ or exc.__context__
    if cause is not None and _depth < 4:
        return f"{text} <- {_describe_exception(cause, _depth + 1)}"
    return text


def mcp_call_sync(
    server: str, tool_name: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Blocking MCP tool invocation returning a normalized dict envelope.

    Never raises — transport and protocol failures are converted into a
    structured error payload so the agent can degrade gracefully (SDD §5.5).
    """
    try:
        payload = _run_coroutine_blocking(mcp_call(server, tool_name, args))
    except McpNotConfigured as exc:
        return {
            "status": "MCP_NOT_CONFIGURED",
            "code": "MCP_TOKEN_MISSING",
            "server": server,
            "tool": tool_name,
            "message": str(exc),
        }
    except TimeoutError as exc:
        return {
            "status": "MCP_ERROR",
            "code": "MCP_TIMEOUT",
            "server": server,
            "tool": tool_name,
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "status": "MCP_ERROR",
            "code": "MCP_TRANSPORT_ERROR",
            "server": server,
            "tool": tool_name,
            "message": _describe_exception(exc),
        }

    if isinstance(payload, dict):
        return payload
    return {"status": "SUCCESS", "result": payload}
