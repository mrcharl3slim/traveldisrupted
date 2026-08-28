"""Overwrite the fixtures with real API responses. Needs network and keys.

    DOWNSTREAM_PORTS=record python record.py              # everything
    DOWNSTREAM_PORTS=record python record.py hotels rates # just these

Keys come from .env, which env.py loads. Jobs: rail, duffel, status, outbound,
onward, hotels, rates.

Run this OUTSIDE the sandbox -- the Cowork container and the device shell both
sit behind an egress allowlist that blocks all three providers, which is why
the committed fixtures started life hand-authored in each API's wire shape
rather than recorded. Everything downstream of the ports parses them exactly as
it will parse the real thing, so this script swaps placeholder for evidence
without any other file changing.

Rail needs no key. Start there: it is the one that proves the pipe.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent / "data"),
                str(Path(__file__).parent / "ports")]

if os.environ.get("DOWNSTREAM_PORTS") != "record":
    sys.exit("set DOWNSTREAM_PORTS=record first, or this will only replay")

import aerodatabox, duffel, hotels, rail               # noqa: E402
from demo_trip import CEST, dt                          # noqa: E402

DAY = dt(12, 9, 0)

#: The trip the demo builds from live data: Singapore out, Zurich, Milan by
#: rail, a hotel in Milan. Recorded a few days ahead so the dates are real
#: without being tomorrow.
SOON = datetime.now(CEST) + timedelta(days=21)
LATER = SOON + timedelta(days=2)


def _count(data) -> int:
    if isinstance(data, list):
        return len(data)
    for key in ("connections", "hotels"):
        if isinstance(data.get(key), list):
            return len(data[key])
    inner = data.get("data")
    if isinstance(inner, list):
        return len(inner)
    if isinstance(inner, dict) and isinstance(inner.get("offers"), list):
        return len(inner["offers"])
    return 0


JOBS = {
    "rail": ("rail  Zurich HB -> Milano Centrale",
             lambda: rail.connections("Zurich HB", "Milano Centrale", DAY)),
    "duffel": ("duffel  ZRH -> MXP  (the scenario's replacement leg)",
               lambda: duffel.search("ZRH", "MXP", DAY)),
    "status": ("status  SQ 346", lambda: aerodatabox.status("SQ346", dt(11, 9, 0))),
    "outbound": (f"duffel  SIN -> ZRH  on {SOON:%d %b}  (the trip's first leg)",
                 lambda: duffel.search("SIN", "ZRH", SOON)),
    "onward": (f"duffel  ZRH -> MXP  on {SOON:%d %b}",
               lambda: duffel.search("ZRH", "MXP", SOON)),
    "hotels": (f"liteapi  hotels in Milan  {SOON:%d %b} - {LATER:%d %b}",
               lambda: hotels.catalogue("Milan", "IT", limit=5)),
    "rates": (f"liteapi  rates in Milan  {SOON:%d %b} - {LATER:%d %b}",
              lambda: hotels.stays("Milan", "IT", SOON, LATER, CEST, limit=5)),
}

wanted = [a for a in sys.argv[1:] if not a.startswith("-")] or list(JOBS)
unknown = [w for w in wanted if w not in JOBS]
if unknown:
    sys.exit(f"unknown job(s) {unknown}. Available: {', '.join(JOBS)}")

for name in wanted:
    label, fn = JOBS[name]
    try:
        data = fn()
        print(f"  recorded  {label}  ({_count(data)} results)")
    except Exception as exc:                            # noqa: BLE001
        # Print the provider's own words. A 401 and a 422 need different fixes,
        # and "FAILED" on its own sends you to the wrong one.
        detail = ""
        body = getattr(exc, "read", None)
        if callable(body):
            try:
                detail = body().decode()[:300]
            except Exception:                           # noqa: BLE001
                detail = ""
        print(f"  FAILED    {label}\n            {type(exc).__name__}: {exc}"
              + (f"\n            {detail}" if detail else ""))
