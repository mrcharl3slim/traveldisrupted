"""Getting an alert to a person, and never claiming to have.

The lanes in `plan.py` exist because an agent that reports a phone call as done
has lied to a stranded traveller. This file is the same rule applied to itself:
a channel that is not configured does not silently swallow the message and
report success. `deliver` returns the channels that actually took it, and an
empty list is a real answer that the page is obliged to show.

WHAT IS ACTUALLY WIRED. The log always. A webhook when DOWNSTREAM_WEBHOOK names
one -- that is a Slack incoming webhook, a Discord one, or anything that accepts
a JSON POST, which is the whole of what a hackathon deployment can honestly
promise. Email is deliberately absent: it needs a verified sending domain and a
provider relationship, and a mailer that lands in spam is worse than no mailer,
because it looks delivered on this end.

DELIVERY IS NOT THE PRODUCT. Knowing what to say and when is. Channels are a
few lines each and the reason this file is short.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime

TIMEOUT = 6

#: Sent as one line, because a notification that needs scrolling is a document.
def render(alert, trip_id: str = "") -> str:
    when = f"{alert.closes:%d %b %H:%M}"
    head = {"final": "MISSED", "found": "DETECTED"}.get(alert.kind, f"T-{alert.lead}")
    tail = f"  ({trip_id})" if trip_id else ""
    return f"[{head}] {alert.message}  · deadline {when}{tail}"


def channels() -> dict[str, bool]:
    """What is actually available in this process, as booleans and not as keys.

    Printed in /health so a deployment answers "would this reach me?" before a
    deadline does, rather than after.
    """
    return {
        "log": True,
        "webhook": bool(os.environ.get("DOWNSTREAM_WEBHOOK", "").strip()),
    }


def _webhook(text: str) -> bool:
    url = os.environ.get("DOWNSTREAM_WEBHOOK", "").strip()
    if not url:
        return False
    # Slack and Discord both accept {"text": ...} / {"content": ...}; sending
    # both keys costs nothing and saves a per-vendor branch nobody maintains.
    body = json.dumps({"text": text, "content": text}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        # A dead webhook must not take the watch down with it. The alert is
        # still true and still shown on the page; what failed is one pipe.
        return False


def deliver(alert, trip_id: str = "", log=print) -> list[str]:
    """Send one alert. Returns the channels that took it -- possibly none."""
    text = render(alert, trip_id)
    sent = []
    log(f"{datetime.now().isoformat(timespec='seconds')}  {text}")
    sent.append("log")
    if _webhook(text):
        sent.append("webhook")
    return sent
