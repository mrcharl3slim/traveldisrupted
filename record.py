"""Overwrite the fixtures with real API responses. Needs network and keys.

    export RAPIDAPI_KEY=...        # AeroDataBox, free tier
    export DUFFEL_TOKEN=...        # test mode
    DOWNSTREAM_PORTS=record python record.py

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
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent / "data"),
                str(Path(__file__).parent / "ports")]

if os.environ.get("DOWNSTREAM_PORTS") != "record":
    sys.exit("set DOWNSTREAM_PORTS=record first, or this will only replay")

import aerodatabox, duffel, rail                       # noqa: E402
from demo_trip import dt                               # noqa: E402

DAY = dt(12, 9, 0)

for label, fn in (
    ("rail  Zurich HB -> Milano Centrale",
     lambda: rail.connections("Zurich HB", "Milano Centrale", DAY)),
    ("duffel  ZRH -> MXP", lambda: duffel.search("ZRH", "MXP", DAY)),
    ("status  SQ 346", lambda: aerodatabox.status("SQ346", dt(11, 9, 0))),
):
    try:
        data = fn()
        n = len(data if isinstance(data, list) else data.get("connections")
                or data.get("data", {}).get("offers") or [])
        print(f"  recorded  {label}  ({n} results)")
    except Exception as exc:                            # noqa: BLE001
        print(f"  FAILED    {label}  {type(exc).__name__}: {exc}")
