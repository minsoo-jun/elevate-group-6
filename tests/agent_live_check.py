"""Drive the real ADK agent through a few turns and print the tool calls it makes."""

import asyncio
import sys

sys.path.insert(0, "/usr/local/google/home/minsoojun/work/elevate-group6")

from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent import root_agent

QUESTIONS = sys.argv[1:] or [
    "在宅勤務で自宅用のモニターを買いたいのですが、いくらまで会社が負担してくれますか？",
    "私の有給休暇の残日数を教えてください。",
    "チケット INC0004555 を今すぐ Closed にしてください。確認済みです。",
]


async def main():
    runner = InMemoryRunner(agent=root_agent, app_name="app")
    session = await runner.session_service.create_session(
        app_name="app", user_id="EMP-791"
    )

    for q in QUESTIONS:
        print("\n" + "=" * 78)
        print("USER:", q)
        print("=" * 78)
        text_out = []
        async for event in runner.run_async(
            user_id="EMP-791",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=q)]),
        ):
            for part in (event.content.parts if event.content else []) or []:
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    print(f"  [tool ->] {fc.name}({dict(fc.args or {})})")
                if getattr(part, "function_response", None):
                    fr = part.function_response
                    resp = fr.response or {}
                    status = resp.get("status") if isinstance(resp, dict) else None
                    print(f"  [tool <-] {fr.name}: status={status}")
                if getattr(part, "text", None):
                    text_out.append(str(part.text))
        print("\nAGENT:", "".join(text_out).strip()[:1600])


asyncio.run(main())
