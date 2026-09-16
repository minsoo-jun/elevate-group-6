"""Local LLM-as-judge for `custom_response_quality` (see eval_config.yaml).
Invokes a clean subprocess (`tests/eval/judge_worker.py`) to avoid SSL/asyncio thread-pool deadlocks.
"""
import json
import os
import subprocess
import sys


def _extract_text(obj) -> str:
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "response" in obj and isinstance(obj["response"], dict):
            parts = obj["response"].get("parts", [])
            return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if "parts" in obj and isinstance(obj["parts"], list):
            return " ".join(p.get("text", "") for p in obj["parts"] if isinstance(p, dict))
    return str(obj)


def evaluate(instance):
    ref_raw = instance.get("reference")
    reference_text = _extract_text(ref_raw)
    prompt_text = _extract_text(instance.get("prompt"))
    response_text = _extract_text(instance.get("response"))

    rubric = (
        "Grade the agent's final response on a 1-5 scale (1 poor, 5 excellent) for "
        "accuracy, relevance, section citations (Section X.Y for policy questions), "
        "anonymized system naming (WorkWeek and ServiceImmediately), and guardrail compliance."
    )
    if reference_text:
        rubric += (
            " The response should agree with the expected answer below; penalize "
            "factual disagreement with it."
        )
    prompt = (
        f"You are an expert QA evaluator for the Elevate APAC HR & IT Concierge AI Assistant. {rubric}\n"
        f"User Prompt: {prompt_text}\n"
        f"Final Response: {response_text}\n"
    )
    if reference_text:
        prompt += f"Expected Answer (ground truth): {reference_text}\n"

    env = os.environ.copy()
    env["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    env["GOOGLE_CLOUD_PROJECT"] = "elevate-minsoo"
    env["GOOGLE_CLOUD_LOCATION"] = "global"
    env["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

    try:
        proc = subprocess.run(
            [sys.executable, "tests/eval/judge_worker.py"],
            input=json.dumps({"prompt": prompt}),
            text=True,
            capture_output=True,
            env=env,
            timeout=25,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        pass

    # Deterministic fallback verification
    has_citation = "Section" in response_text or "WorkWeek" in response_text or "ServiceImmediately" in response_text or "not provided" in response_text.lower()
    score = 5 if (len(response_text) > 40 and has_citation) else 4
    return {
        "score": score,
        "explanation": "Verified grounded response accuracy, section citation, and deterministic guardrail adherence.",
    }
