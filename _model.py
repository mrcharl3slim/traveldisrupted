"""One switch decides the provider, exactly as twin/_model.py does.

    LLM_PROVIDER=bedrock | anthropic | groq | none

WHY A SWITCH AND NOT A CHOICE. Bedrock is the sponsor stack and where this is
judged; the Anthropic API iterates in seconds on a laptop at 1 a.m.; Groq is
already in lab/.env. None is worth rewriting the agent for, so the agent never
learns which one it got.

"none" is not a degraded mode to apologise for -- it is the mode the demo runs
in. Every LLM call in this system has a deterministic fallback, because the
parts a judge is asked to believe (EUR 282, the ranking, the deadlines) are
arithmetic, and arithmetic should not become unavailable when a token expires.
The model earns its place on the two jobs code cannot do: reading fare prose,
and explaining a plan to someone who has been awake for fourteen hours.

DEFAULTS COPIED FROM twin/, NOT CHOSEN FRESH. Bedrock model access is granted
per account and per region, and this account has haiku-4-5 working in
ap-southeast-1. An earlier version of this file defaulted to a Sonnet model id
that nobody had enabled -- it would have raised AccessDeniedException on the
first live run, most likely the night before submission. The adaptive retry
config is copied for the same reason: a fresh AWS account has low Bedrock
quota, and a demo asking questions quickly will hit it.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import env as _env  # noqa: E402

_env.load()

PROVIDER = os.environ.get("LLM_PROVIDER", "none").strip().lower()

BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
REGION = (os.environ.get("AWS_DEFAULT_REGION")
          or os.environ.get("AWS_REGION") or "ap-southeast-1")


def label() -> str:
    return {"bedrock": f"bedrock:{BEDROCK_MODEL}",
            "anthropic": f"anthropic:{ANTHROPIC_MODEL}",
            "groq": f"groq:{GROQ_MODEL}"}.get(PROVIDER, "none (arithmetic only)")


def available() -> bool:
    return PROVIDER in ("bedrock", "anthropic", "groq")


def get_model(temperature: float = 0.0):
    """A LangChain chat model, or None when we are running on arithmetic."""
    if PROVIDER == "bedrock":
        import boto3
        from botocore.config import Config
        from langchain_aws import ChatBedrockConverse
        client = boto3.client("bedrock-runtime", config=Config(
            region_name=REGION, read_timeout=120, connect_timeout=30,
            retries={"max_attempts": 4, "mode": "adaptive"}))
        return ChatBedrockConverse(model_id=BEDROCK_MODEL, client=client,
                                   temperature=temperature)
    if PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=ANTHROPIC_MODEL, temperature=temperature,
                             timeout=30)
    if PROVIDER == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=GROQ_MODEL, temperature=temperature)
    return None
