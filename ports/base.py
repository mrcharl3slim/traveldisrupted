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
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Before MODE and before any port reads a token: a .env that nothing loads is
# a credential that silently does not exist.
sys.path.insert(0, str(FIXTURES.parent))
import env as _env  # noqa: E402

_env.load()

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


def _pick(name: str, candidates: list[Path], prefer: date | None,
          wanted: date | None) -> Path:
    """Which of several recordings of the same route to replay.

    The first version refused outright: two recordings differing only by date
    meant there was no way to know which was meant, so it raised and told you to
    re-record. That was right about the risk and wrong about the remedy --
    recording again adds a THIRD candidate and the next run fails the same way,
    which is how the scripted demo broke the morning after a second ZRH -> MXP
    search was captured for the booking flow.

    Two rules instead:

      * a caller that knows which recording it means says so. The scripted
        demo's replacement leg is pinned by tests and shown to judges, so it
        names the October capture and keeps its figures whatever date the trip
        is anchored to. That is what makes the demo immune to this choice
        rather than dependent on it.
      * anything else takes the recording nearest the day being asked about,
        because the shift is the distortion and the smallest one is the least
        wrong. A booking search three weeks out lands on the capture made for
        three weeks out, not on a scenario fixture from six weeks later.

    Note what the date in a filename is: the day the search was FOR, not the day
    it was captured. So "the newest file" is not "the freshest evidence" -- it
    is whichever recording reaches furthest into the calendar, which justifies
    nothing. Nearest is the rule that survives knowing that.

    Either way the choice goes into `shifted` and onto the banner. The original
    worry was a silent wrong answer; this answer is not silent.
    """
    if len(candidates) == 1:
        return candidates[0]
    if prefer is not None:
        for candidate in candidates:
            if _first_date(candidate.stem) == prefer:
                return candidate
    if wanted is None:
        return candidates[0]          # sorted: stable, and nothing better to go on
    return min(candidates,
               key=lambda p: (abs(((_first_date(p.stem) or date.min) - wanted).days),
                              p.name))


def _replay(name: str, key: dict[str, Any], prefer: date | None = None) -> Any:
    """The recording for this key, or one that differs from it only by date.

    The scenario is anchored to run time so that live flight status -- which
    only covers about a week either side of today -- can be demonstrated at all.
    That moves every fixture lookup off the date the recordings were captured
    on. Rather than re-record before every demo, a date-shifted match is
    accepted and its dates are moved to suit; `_pick` decides which one when
    there is more than one, and the shift is announced either way.
    """
    exact = _path(name, key)
    if exact.exists():
        return json.loads(exact.read_text())

    wanted = _slug(key)
    pattern = _ISO.sub("DATE", wanted)
    folder = FIXTURES / name
    candidates = sorted(p for p in folder.glob("*.json")
                        if _ISO.sub("DATE", p.stem) == pattern) if folder.exists() else []

    if not candidates:
        raise PortError(
            f"no recording at {exact.relative_to(FIXTURES.parent)}. Run "
            "`DOWNSTREAM_PORTS=record python record.py` on a machine with "
            "network access.")

    want = _first_date(wanted)
    found = _pick(name, candidates, prefer, want)
    have = _first_date(found.stem)
    days = (want - have).days if (want and have) else 0
    among = (f", chosen from {len(candidates)} recordings of this route"
             if len(candidates) > 1 else "")
    shifted.append(f"{name}: replayed {found.name} shifted {days:+d} days{among}")
    return json.loads(_shift_dates(found.read_text(), days))


#: How long to wait on one provider before giving up and replaying instead.
#:
#: Short on purpose. A recording exists for everything the demo touches, so the
#: cost of giving up early is a `degraded` badge and a fare that is minutes
#: rather than seconds old. The cost of waiting is a hosting gateway returning
#: its own error page over the top of us, which the browser then cannot read as
#: JSON -- a failure that looks like a bug in this code and is not.
TIMEOUT = int(os.environ.get("DOWNSTREAM_TIMEOUT", "8"))


def url_for(base: str, path: str = "", **params: Any) -> str:
    """Build a URL with the query string encoded, once, in one place.

    THIS IS THE FUNCTION REPLAY CANNOT TEACH YOU TO WRITE. In replay `call`
    never invokes the lambda that builds a URL, so every port shipped its own
    interpolation and none of them were ever executed by a test. Three ports,
    three different degrees of wrong: hotels interpolated the city raw and
    crashed on "New York"; rail hand-rolled `.replace(' ', '%20')`, which
    survives a space and mangles "Zürich HB"; status stripped spaces out of a
    flight number, which is right for that field and encodes nothing else.

    A space is only the first character that needs this. Accents, ampersands,
    plus signs and slashes all appear in real place names, and each one fails
    differently -- some raise, some silently query for the wrong thing, which
    is worse. `urlencode` knows all of them.

    ``path`` segments are quoted too, because a city can end up in a path as
    easily as in a query.
    """
    from urllib.parse import quote, urlencode

    if path:
        base = base.rstrip("/") + "/" + "/".join(
            quote(str(part), safe="") for part in path.strip("/").split("/"))
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return f"{base}?{urlencode(clean)}" if clean else base


def get_json(url: str, headers: dict[str, str] | None = None,
             body: bytes | None = None, timeout: int | None = None) -> Any:
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        return json.loads(r.read().decode())


#: How many of each list to keep when recording. The recordings are committed
#: as evidence, and a provider returning ninety-four offers of which the engine
#: uses four is seven megabytes of repository nobody reads.
#:
#: Shape is preserved exactly — same keys, same nesting, same types — because
#: the point of a recording is that it parses identically to the live response.
#: Only the count changes, and the trimming keeps a spread across the day rather
#: than the first N, or an afternoon cancellation would find no afternoon
#: replacements and the demo would quietly have no options.
KEEP = {"duffel": 24, "rail": 12, "rates": 1}


def _spread(items: list, keep: int) -> list:
    """``keep`` items spaced evenly across the list, endpoints included."""
    if len(items) <= keep:
        return items
    step = (len(items) - 1) / (keep - 1)
    return [items[round(i * step)] for i in range(keep)]


def trim(name: str, data: Any) -> Any:
    """Shrink a recording without changing what it looks like."""
    if name == "duffel":
        offers = (data.get("data") or {}).get("offers")
        if isinstance(offers, list):
            ordered = sorted(offers, key=lambda o: (
                o.get("slices", [{}])[0].get("segments", [{}])[0].get("departing_at", "")))
            data["data"]["offers"] = _spread(ordered, KEEP["duffel"])
    elif name == "rail":
        if isinstance(data.get("connections"), list):
            data["connections"] = _spread(data["connections"], KEEP["rail"])
    elif name == "rates":
        for entry in (data.get("data") or []):
            rooms = entry.get("roomTypes")
            if isinstance(rooms, list) and rooms:
                entry["roomTypes"] = rooms[:KEEP["rates"]]
                for room in entry["roomTypes"]:
                    if isinstance(room.get("rates"), list):
                        room["rates"] = room["rates"][:KEEP["rates"]]
    return data


def call(name: str, key: dict[str, Any], fetch: Callable[[], Any],
         prefer: date | None = None) -> Any:
    """Fetch or replay, according to MODE. Never raises in replay-backed paths.

    ``prefer`` names the capture date a caller means when several recordings of
    the same request exist. It is a hint for replay only -- a live call has no
    use for it, and a recording overwrites whichever file the key names.
    """
    fixture = _path(name, key)

    if MODE == "replay":
        return _replay(name, key, prefer)

    try:
        data = fetch()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        try:
            data = _replay(name, key, prefer)
        except PortError:
            raise PortError(f"{name}: live call failed and no recording exists: {exc}")
        degraded.append(f"{name}: live call failed ({type(exc).__name__}), replayed a recording")
        return data

    if MODE == "record":
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(trim(name, data), indent=2, ensure_ascii=False))
    return data
