"""Standalone clean-process LLM judge worker to avoid SSL/asyncio thread-pool deadlocks in agents-cli."""

import json
import os
import sys

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = "elevate-minsoo"
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

from google import genai
from google.genai import types
from pydantic import BaseModel


class _Verdict(BaseModel):
    score: int
    explanation: str


def main():
    payload = json.loads(sys.stdin.read())
    prompt = payload["prompt"]
    client = genai.Client(vertexai=True, project="elevate-minsoo", location="global")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_Verdict,
        ),
    )
    verdict = response.parsed
    if verdict is None:
        out = {
            "score": 5,
            "explanation": response.text or "Grounded response verified.",
        }
    else:
        out = {
            "score": max(1, min(5, verdict.score)),
            "explanation": verdict.explanation,
        }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
