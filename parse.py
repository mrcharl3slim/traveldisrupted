"""Confirmation prose -> Booking, with fare rules resolved to deadlines.

THIS IS THE ONLY PLACE THE MODEL IS LOAD-BEARING, and it is load-bearing for a
reason no API can replace. "Free change up to 4 hours before pickup" is a
sentence. Turning it into 12 Oct 06:00 requires reading it, knowing which
booking it modifies, and doing arithmetic on that booking's own times. Duffel
will not tell you. AeroDataBox will not tell you. It is the moat.

TWO RULES THAT KEEP IT HONEST.

1. The model returns windows as OFFSETS ("4 hours before start"), never as
   absolute timestamps. Resolution to a datetime happens here, in Python,
   against the booking's real times. A model that is allowed to emit
   "2026-10-12T06:00:00" is a model that can silently emit the wrong day, and
   nothing downstream would ever know.

2. Every Policy keeps ``source`` -- the sentence it was derived from. When the
   engine claims EUR 48 is recoverable until 06:00, the prose that justifies it
   travels with the claim and can be shown to whoever doubts it.

Anything the model cannot parse becomes a Policy with no windows, which the
engine treats as non-refundable. Failing towards "you cannot get this money
back" is the safe direction: it under-promises rather than inventing a refund
that does not exist.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from policy import Policy, Window

SYSTEM = """You read travel booking confirmations and extract refund and change rules.

Return ONLY a JSON object:
{"windows": [{"offset_hours_before": <number>, "anchor": "start"|"deadline",
              "refund": <number>, "fee": <number>, "label": "<provider's own wording>"}],
 "note": "<one clause on anything ambiguous>"}

Rules:
- offset_hours_before is hours BEFORE the anchor. "up to 4 hours before pickup" -> 4.
  "before departure" -> 0. A date-change allowed until the event itself -> 0.
- refund is money returned; fee is money it costs to act. A EUR 18 date change on a
  EUR 92 ticket is refund 92, fee 18 (the traveller keeps the value, minus the fee).
- NEVER output absolute dates or times. Offsets only.
- If the text says non-refundable with no escape at all, return {"windows": [], ...}.
- Quote the provider's wording in label. Do not paraphrase it into something kinder."""


def _resolve(raw: dict, start: datetime, deadline: datetime | None) -> list[Window]:
    windows = []
    for w in raw.get("windows", []):
        anchor = deadline if (w.get("anchor") == "deadline" and deadline) else start
        try:
            hours = float(w.get("offset_hours_before", 0))
            windows.append(Window(
                closes=anchor - timedelta(hours=hours),
                refund=float(w.get("refund", 0) or 0),
                fee=float(w.get("fee", 0) or 0),
                label=str(w.get("label", ""))[:120]))
        except (TypeError, ValueError):
            continue          # unparseable window -> treated as no escape
    return windows


def parse_policy(text: str, start: datetime, deadline: datetime | None = None,
                 model=None) -> Policy:
    """Prose -> Policy. Falls back to non-refundable, never to a guess."""
    if model is None:
        return Policy(source=text)

    try:
        reply = model.invoke(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": text}])
        body = reply.content if isinstance(reply.content, str) else str(reply.content)
        match = re.search(r"\{.*\}", body, re.S)
        raw = json.loads(match.group(0)) if match else {}
    except Exception:                                   # noqa: BLE001
        return Policy(source=text)

    return Policy(windows=_resolve(raw, start, deadline), source=text)
