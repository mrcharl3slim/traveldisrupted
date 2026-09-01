"""Flight status, via AeroDataBox on RapidAPI. Free tier: 600 units a month.

A CONSTRAINT WORTH KNOWING BEFORE DEMO DAY. Flight status is a near-real-time
product -- roughly a week either side of today. The scenario is dated 12
October; on 7 September no status API on earth has an opinion about it. Live
detection therefore requires the trip to be anchored relative to run time
rather than to a fixed date. See build_trip() in data/demo_trip.py.
"""

from __future__ import annotations

import os
from datetime import datetime

from base import call, get_json, url_for

HOST = "aerodatabox.p.rapidapi.com"


def _headers():
    return {"X-RapidAPI-Key": os.environ.get("RAPIDAPI_KEY", ""),
            "X-RapidAPI-Host": HOST}


#: Live status calls spent today, by UTC day. The free tier is 600 units a
#: MONTH; a one-minute watch over three trips with three imminent flights each
#: spends 54 an hour and the month by lunchtime. The budget is a day's
#: allowance (default 120 -- five demo days of headroom), checked only on the
#: live path: replay is free, and exhaustion is not an outage -- the meter
#: raises into `base.call`'s existing fallback, so the answer comes from a
#: recording and /health says "degraded" instead of the quota dying silently.
_spent: dict[str, int] = {}


def _metered_get(url: str):
    budget = int(os.environ.get("DOWNSTREAM_ADB_DAILY_BUDGET", "120"))
    day = f"{datetime.utcnow():%Y-%m-%d}"
    for gone in [d for d in _spent if d != day]:
        _spent.pop(gone, None)
    if _spent.get(day, 0) >= budget:
        raise TimeoutError(
            f"aerodatabox: daily budget of {budget} live calls spent")
    _spent[day] = _spent.get(day, 0) + 1
    return get_json(url, headers=_headers())


def status(flight: str, day: datetime):
    key = {"flight": flight.replace(" ", ""), "on": f"{day:%Y-%m-%d}"}
    # The space really is stripped rather than encoded: "SQ 346" and "SQ346"
    # are the same flight and this API wants the second. Everything after that
    # is quoted like any other path segment.
    url = url_for(f"https://{HOST}", f"flights/number/"
                  f"{flight.replace(' ', '')}/{day:%Y-%m-%d}")
    return call("status", key, lambda: _metered_get(url))


def disruption(flight: str, day: datetime, booking_id: str):
    """Wire format -> Disruption, or None when the flight is running to plan.

    Predicted arrival is preferred over actual: the entire value of this
    product is acting while the connection can still be saved, and by the time
    an actual arrival exists the EUR 48 transfer window closed hours ago.
    """
    from domain import Disruption

    rows = status(flight, day)
    if not rows:
        return None
    row = rows[0]
    arr = row.get("arrival", {})
    predicted = (arr.get("predictedTime") or arr.get("actualTime") or {}).get("local")
    scheduled = (arr.get("scheduledTime") or {}).get("local")
    if not predicted or not scheduled:
        return None

    new_end = datetime.fromisoformat(predicted.replace(" ", "T"))
    was = datetime.fromisoformat(scheduled.replace(" ", "T"))
    if new_end <= was:
        return None
    return Disruption(
        booking_id=booking_id, new_end=new_end,
        reason=row.get("status", "delayed").lower(),
        confidence=0.91 if arr.get("predictedTime") else 1.0)
