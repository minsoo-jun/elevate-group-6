"""MCP connectivity check for the Unified Mock Enterprise Services.

Usage:
    uv run python tests/mcp_client_check.py
    uv run python tests/mcp_client_check.py <mcp_url> <token>

Verifies that both the WorkWeek and ServiceImmediately Streamable HTTP MCP
servers are reachable with the configured `X-MCP-Token` and prints the tool
catalog discovered on each.
"""

from __future__ import annotations

import asyncio
import sys

from app.tools.mcp_client import (
    MCP_BASE_URL,
    MCP_TOKEN,
    list_mcp_tools,
    mcp_enabled,
    server_url,
)

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)


async def check_server(label: str, server: str) -> bool:
    url = server_url(server)
    print(f"\n{DIM}── {label}{RESET}")
    print(f"   {DIM}{url}{RESET}")
    try:
        tools = await list_mcp_tools(server)
    except Exception as exc:
        print(f"   {RED}✗ FAILED{RESET}  {type(exc).__name__}: {exc}")
        return False
    print(f"   {GREEN}✓ CONNECTED{RESET}  {len(tools)} tools discovered")
    for t in tools:
        desc = t["description"][:76]
        print(f"      {DIM}•{RESET} {t['name']:<28} {DIM}{desc}{RESET}")
    return True


async def check_explicit(url: str, token: str) -> bool:
    """Check an explicitly supplied URL + token pair (parity with the spec's CLI)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    print(f"\n{DIM}── explicit target{RESET}\n   {DIM}{url}{RESET}")
    try:
        async with streamablehttp_client(url, headers={"X-MCP-Token": token}) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as session:
                await session.initialize()
                resp = await session.list_tools()
                print(
                    f"   {GREEN}✓ CONNECTED{RESET}  {len(resp.tools)} tools discovered"
                )
                for t in resp.tools:
                    print(f"      {DIM}•{RESET} {t.name}")
        return True
    except Exception as exc:
        print(f"   {RED}✗ FAILED{RESET}  {type(exc).__name__}: {exc}")
        return False


async def main() -> int:
    print(f"\n{DIM}MCP Connectivity Check — Unified Mock Enterprise Services{RESET}")
    print(f"{DIM}base: {MCP_BASE_URL}{RESET}")

    if len(sys.argv) >= 3:
        ok = await check_explicit(sys.argv[1], sys.argv[2])
        return 0 if ok else 1

    if not mcp_enabled():
        print(f"\n{YELLOW}⚠ MCP_TOKEN is not configured.{RESET}")
        print(
            f"  Add {DIM}MCP_TOKEN=mcp_your_token_here{RESET} to .env (see env.md), then re-run."
        )
        return 2

    masked = MCP_TOKEN[:8] + "…" + MCP_TOKEN[-4:] if len(MCP_TOKEN) > 14 else "set"
    print(f"{DIM}token: {masked}{RESET}")

    results = [
        await check_server("WorkWeek (HCM)", "workweek"),
        await check_server("ServiceImmediately (ITSM)", "serviceimmediately"),
    ]

    print()
    if all(results):
        print(f"{GREEN}✓ All MCP servers reachable.{RESET}\n")
        return 0
    print(f"{RED}✗ One or more MCP servers unreachable.{RESET}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
