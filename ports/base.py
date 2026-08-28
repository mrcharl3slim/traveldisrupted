"""Record and replay, so a live demo cannot be killed by a live API.

THE POSTURE, borrowed from twin/store.py. A port has three modes and a strong
opinion about which one runs on stage:

    replay (default)  read the recorded response. No network, no latency, no
                      rate limit, no surprise. This is what runs in front of
                      judges and in CI.
    live              call the API. If the call fails, fall back to the
                      recording and SAY SO -- degraded and working beats
                      correct and dead, but only if the degradation is visible.
    record            call the API and overwrite the recording.

WHY THIS EXISTS HERE AND NOT AS A NICE-TO-HAVE. The whole product is a
countdown against real deadlines. A four-minute demo that spends forty seconds
on a cold Duffel request has lost the room, and one that raises a timeout in
front of a judge has lost more than that. Recording real responses once, and
replaying them forever, is how "all four APIs are live" and "the demo always
runs" stop being in tension.

The recordings are committed on purpose. They are evidence: the shape of what
each provider actually returned, checkable by anyone who doubts the numbers.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MODE = os.environ.get("DOWNSTREAM_PORTS", "replay").strip().lower()

# Ports that fell back to a recording after a live call failed. Surfaced by
# /health and printed by the CLI: a stale number that nobody flags is worse
# than no number at all.
degraded: list[str] = []

# Recordings replayed under a different date than they were captured on. Same
# rule as `degraded`: the adjustment is legitimate, and invisible adjustment is
# not. Surfaced by /health and the CLI.
shifted: list[str] = []

# Not \b at the ends: fixture slugs put "_" next to the date, and "_" counts as
# a word character, so \b never fires there. Digit lookarounds are what is
# actually meant -- do not match a date inside a longer run of digits.
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


class PortError(RuntimeError):
    pass


def _slug(key: dict[str, Any]) -> str:
    raw = "_".join(f"{k}-{key[k]}" for k in sorted(key))
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw)[:120]


def _path(name: str, key: dict[str, Any]) -> Path:
    return FIXTURES / name / f"{_slug(key)}.json"


def _first_date(text: str) -> date | None:
    m = _ISO.search(text)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _shift_dates(text: str, days: int) -> str:
    """Move every ISO date in a recorded payload by ``days``.

    Replaying October's recording for a trip that now runs next Tuesday means
    its timestamps have to move too, or the engine compares a rail departure in
    October with a flight arrival next week and produces a confident,
    meaningless answer. Whole dates only -- times of day, durations and every
    interval between them are left exactly as the provider returned them.
    """
    if not days:
        return text

    def bump(m: re.Match) -> str:
        moved = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) + timedelta(days=days)
        return moved.isoformat()

    return _ISO.sub(bump, text)


def _replay(name: str, key: dict[str, Any]) -> Any:
    """The recording for this key, or the one recording that differs only by date.

    The scenario is anchored to run time so that live flight status -- which
    only covers about a week either side of today -- can be demonstrated at all.
    That moves every fixture lookup off the date the recordings were captured
    on. Rather than re-record before every demo, an unambiguous match is
    accepted and its dates are shifted to suit.

    Unambiguous is the whole safeguard: if two recordings in a directory differ
    only by date, there is no way to know which one was meant, and guessing
    would silently answer with the wrong day's flight.
    """
    exact = _path(name, key)
    if exact.exists():
        return json.loads(exact.read_text())

    wanted = _slug(key)
    pattern = _ISO.sub("DATE", wanted)
    folder = FIXTURES / name
    candidates = [p for p in folder.glob("*.json")
                  if _ISO.sub("DATE", p.stem) == pattern] if folder.exists() else []

    if len(candidates) != 1:
        raise PortError(
            f"no recording at {exact.relative_to(FIXTURES.parent)}"
            + (f" ({len(candidates)} date-shifted candidates -- too ambiguous to pick)"
               if candidates else "")
            + ". Run `DOWNSTREAM_PORTS=record python record.py` on a machine "
              "with network access.")

    found = candidates[0]
    want, have = _first_date(wanted), _first_date(found.stem)
    days = (want - have).days if (want and have) else 0
    shifted.append(f"{name}: replayed {found.name} shifted {days:+d} days")
    return json.loads(_shift_dates(found.read_text(), days))


def get_json(url: str, headers: dict[str, str] | None = None,
             body: bytes | None = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def call(name: str, key: dict[str, Any], fetch: Callable[[], Any]) -> Any:
    """Fetch or replay, according to MODE. Never raises in replay-backed paths."""
    fixture = _path(name, key)

    if MODE == "replay":
        return _replay(name, key)

    try:
        data = fetch()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        try:
            data = _replay(name, key)
        except PortError:
            raise PortError(f"{name}: live call failed and no recording exists: {exc}")
        degraded.append(f"{name}: live call failed ({type(exc).__name__}), replayed a recording")
        return data

    if MODE == "record":
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data
