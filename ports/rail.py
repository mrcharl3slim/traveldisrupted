"""Swiss rail, via transport.opendata.ch. Free, no key, real timetable.

ONE THING THIS API DOES NOT SELL, and it matters more than it looks: fares.
transport.opendata.ch returns departures, arrivals, transfers and rolling stock
-- not a price. SBB publishes no free fare API either. So the EUR 72 that makes
Plan B come out at -EUR 14 cannot be quoted live by anything we can reach.

We do not paper over that. Every Offer carries ``price_source``, either
"quoted" (a real fare from a real booking API) or "estimate" (ours, with the
deep link so the traveller sees the true number before paying). The plan
comparison shows which is which. A product whose entire pitch is honesty about
what it can and cannot touch does not get to quietly invent a fare.
"""

from __future__ import annotations

import re
from datetime import datetime

from base import call, get_json

BASE = "https://transport.opendata.ch/v1/connections"


def _iso(stamp: str) -> datetime:
    """transport.opendata.ch stamps offsets as "+0200", without the colon.

    Older Pythons reject that outright and newer ones only started accepting it
    in 3.11, so a deployment target we do not control could break on a string
    the API has always sent. Normalising here, at the boundary, is a two-line
    fix; discovering it on a Render box the night before submission is not.
    """
    return datetime.fromisoformat(
        re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", stamp))

# Second-class SBB saver fares, Zurich to Milan, observed range EUR 62-89. The
# midpoint is what we show, clearly labelled, until a fare API exists.
ESTIMATE_EUR = 72.0
DEEP_LINK = "https://www.sbb.ch/en/buying/pages/fahrplan/fahrplan.xhtml"


def connections(origin: str, destination: str, when: datetime, limit: int = 4):
    key = {"from": origin, "to": destination, "at": when.strftime("%Y-%m-%dT%H:%M")}
    url = (f"{BASE}?from={origin.replace(' ', '%20')}"
           f"&to={destination.replace(' ', '%20')}"
           f"&date={when:%Y-%m-%d}&time={when:%H:%M}&limit={limit}")
    return call("rail", key, lambda: get_json(url))


def offers(origin: str, destination: str, when: datetime, station_map: dict):
    """Wire format -> Offer. Direct services only: a plan whose hero number
    rests on a same-day transfer in a foreign station is not a plan we should
    be recommending to somebody who has just been awake for fourteen hours."""
    from offers import Offer

    out = []
    for c in connections(origin, destination, when).get("connections", []):
        if int(c.get("transfers", 0)) != 0:
            continue
        dep = _iso(c["from"]["departure"])
        arr = _iso(c["to"]["arrival"])
        product = (c.get("products") or ["train"])[0]
        out.append(Offer(
            id=f"rail-{dep:%H%M}", mode="rail", carrier="SBB",
            label=f"{product} {dep:%H:%M} - {c['from']['station']['name']} "
                  f"to {c['to']['station']['name']}",
            depart=dep, arrive=arr,
            origin=station_map.get(c["from"]["station"]["name"], "ZRH_HB"),
            destination=station_map.get(c["to"]["station"]["name"], "MILANO_C"),
            price=ESTIMATE_EUR, price_source="estimate", book_url=DEEP_LINK,
        ))
    return out
