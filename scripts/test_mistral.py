"""Test the Mistral fallback provider.

Checks three things in order, because they fail for different reasons:
  1. the key authenticates at all
  2. which models the key can actually reach
  3. whether the configured model handles nested structured output, which is
     what evaluate_candidate() actually needs

Usage:  python scripts/test_mistral.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

load_dotenv()

KEY = (os.getenv("MISTRAL_API_KEY") or "").strip()
MODEL = (os.getenv("MISTRAL_MODEL") or "open-mistral-nemo").strip()

if not KEY:
    sys.exit("MISTRAL_API_KEY not set in .env")

print(f"model from .env: {MODEL}\n")


# ---------------------------------------------------------------- 1. auth
print("[1] authenticating and listing models...")
try:
    r = httpx.get(
        "https://api.mistral.ai/v1/models",
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=30,
    )
    if r.status_code == 401:
        sys.exit("  [FAIL] 401 - key is invalid or revoked")
    if r.status_code == 403:
        sys.exit(
            "  [FAIL] 403 - key rejected. Mistral's free tier requires phone\n"
            "         verification; check console.mistral.ai"
        )
    r.raise_for_status()
    ids = sorted(m["id"] for m in r.json()["data"])
    print(f"  [ok] key valid, {len(ids)} models visible")
    print(f"  configured model present: {MODEL in ids}")
    if MODEL not in ids:
        print(f"  available: {', '.join(ids[:15])}")
except SystemExit:
    raise
except Exception as exc:
    sys.exit(f"  [FAIL] {exc}")


# --------------------------------------------------- 2. plain completion
print("\n[2] plain completion...")
try:
    r = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 10,
            "temperature": 0,
        },
        timeout=60,
    )
    if r.status_code == 429:
        print("  [warn] 429 rate limited - transient, tenacity backoff covers this")
    elif r.status_code == 403:
        sys.exit(f"  [FAIL] 403 - {MODEL} not available on your tier. Try another id.")
    else:
        r.raise_for_status()
        print(f"  [ok] responded: {r.json()['choices'][0]['message']['content']!r}")
except SystemExit:
    raise
except Exception as exc:
    sys.exit(f"  [FAIL] {exc}")


# ------------------------------------- 3. nested structured output (the real test)
print("\n[3] nested structured output via LangChain...")


# Use the REAL Dimension so this genuinely tests the production schema, which
# now truncates overlong strings at a word boundary instead of rejecting them.
from backend.models.schemas import _truncate_at_word  # noqa: E402
from backend.models.schemas import Dimension  # noqa: E402


class MiniEvaluation(BaseModel):
    """Same shape as ResumeEvaluation - nested Dimension objects."""

    skills_match: Dimension
    project_depth: Dimension
    summary: str = Field(description="2-3 sentences, at most ~400 characters")

    @field_validator("summary", mode="before")
    @classmethod
    def _cap_summary(cls, v: object) -> object:
        return _truncate_at_word(v, 400)


try:
    from langchain_mistralai import ChatMistralAI
except ImportError:
    sys.exit("  [FAIL] pip install langchain-mistralai")

try:
    llm = ChatMistralAI(model=MODEL, api_key=KEY, temperature=0)
    structured = llm.with_structured_output(MiniEvaluation)
    out = structured.invoke(
        "Job: ML Engineer needing PyTorch and deployment experience.\n"
        "Candidate resume: 'Built a CNN image classifier in PyTorch. "
        "Deployed it as a REST API with FastAPI.'\n"
        "Score skills_match and project_depth 0-10 with reasoning and evidence "
        "quoted from the resume."
    )
    print(f"  [ok] parsed into {type(out).__name__}")
    print(f"       skills_match : {out.skills_match.score}  {out.skills_match.reasoning[:70]}")
    print(f"       project_depth: {out.project_depth.score}  {out.project_depth.reasoning[:70]}")
    print(f"       evidence     : {out.skills_match.evidence}")
    print("\n  Nested structured output works. Usable as a fallback.")
except Exception as exc:
    msg = str(exc)
    print(f"  [FAIL] {msg[:220]}")
    if "429" in msg:
        print("\n  Rate limited. Re-run in a minute; this alone doesn't rule the model out.")
    elif "tool" in msg.lower() or "schema" in msg.lower() or "function" in msg.lower():
        print(
            "\n  Schema/tool-call failure. open-mistral-nemo is 12B and can struggle\n"
            "  with nested schemas. Try MISTRAL_MODEL=mistral-small-latest, which\n"
            "  handles nesting more reliably."
        )
    sys.exit(1)