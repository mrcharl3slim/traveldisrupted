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
    """What was ASKED for. Not the same question as whether it works."""
    return PROVIDER in ("bedrock", "anthropic", "groq")


def effective() -> dict:
    """What is actually there, having tried to build it.

    `label` reports configuration, which is the wrong thing to put on a status
    page on its own. Set LLM_PROVIDER=bedrock with a region that has not been
    granted the model and the banner says "bedrock:claude-haiku-4-5" in
    confident blue while every call quietly falls back to a template. Nobody
    finds out until a judge asks what the model is doing.

    So this builds it -- cached, so the cost is paid once -- and reports the
    difference between asked-for and available. Everywhere else in this system
    a degradation is announced (`degraded` when a port replays, `shifted` when
    a recording moves, which channels actually took a message). This is the
    same rule applied to the model.
    """
    model = get_model()
    return {
        "configured": PROVIDER,
        "label": label(),
        # CONSTRUCTED, not proven. Building a client resolves no credentials
        # and calls nothing, so this says the wiring is present -- not that
        # Bedrock will answer. A key with no model access still reads True
        # here and fails on the first invoke, which is why `note_failure`
        # exists: the first real call that fails writes its reason in, and the
        # page stops claiming a model it does not have.
        "ready": model is not None and not unavailable,
        # Empty unless something failed. A provider of "none" is a choice, not
        # a fault, and must not be reported as one.
        "why_not": unavailable,
    }


def note_failure(exc: BaseException) -> None:
    """Record a model call that failed, so /health stops saying it is fine.

    Called from the places that catch model exceptions and carry on. Carrying
    on is right -- the arithmetic does not need the model -- but doing it
    silently means a broken token looks identical to a working one for as long
    as nobody reads the phrasing closely.
    """
    global unavailable
    unavailable = f"{PROVIDER}: {type(exc).__name__}: {exc}"[:200]


#: Why the model is unavailable, when it is. Surfaced by /health rather than
#: raised: a missing wheel or an expired token must degrade the phrasing, not
#: the arithmetic.
unavailable: str = ""

_CACHE: dict[float, object] = {}


def get_model(temperature: float = 0.0):
    """A LangChain chat model, or None when we are running on arithmetic.

    Built once and kept. A conversational front door calls this on every turn,
    and constructing a fresh boto3 client per keystroke spends more time on
    credential resolution than on thinking.

    Never raises. The provider SDK may not be installed, the region may not
    have the model enabled, the credentials may have expired at 2 a.m. -- all
    of which are reasons to answer with a templated sentence, and none of which
    are reasons for a traveller's booking to return a 500. The reason is kept
    in `unavailable` so /health can say what happened instead of pretending the
    system was always meant to run this way.
    """
    global unavailable
    if temperature in _CACHE:
        return _CACHE[temperature]
    try:
        model = _build(temperature)
    except Exception as exc:                            # noqa: BLE001
        unavailable = f"{PROVIDER}: {type(exc).__name__}: {exc}"[:200]
        model = None
    _CACHE[temperature] = model
    return model


def _build(temperature: float):
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
