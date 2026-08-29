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
import unicodedata
from datetime import datetime, timedelta

from base import call, get_json, url_for

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


#: Ask for more than we show. `offers` keeps direct services only — a plan whose
#: hero number rests on changing trains in a foreign station at midnight is not
#: a plan — and on a real timetable the first four results are often all
#: connecting. Asking for four and filtering to direct returned nothing at all,
#: which reads as "no rail option exists" when the truth is "we did not look far
#: enough down the list".
def connections(origin: str, destination: str, when: datetime, limit: int = 12):
    key = {"from": origin, "to": destination, "at": when.strftime("%Y-%m-%dT%H:%M")}
    url = url_for(BASE, **{"from": origin, "to": destination,
                           "date": f"{when:%Y-%m-%d}", "time": f"{when:%H:%M}",
                           "limit": limit})
    return call("rail", key, lambda: get_json(url))


#: At most one change, and only with real time to make it. The original rule was
#: no changes at all — a plan resting on a same-day transfer in a foreign station
#: is not a plan for somebody awake fourteen hours. Against the actual timetable
#: that rule deleted the entire mode: there is no direct Zurich-Milano service,
#: because the EC terminates at the border and a regional continues. One change
#: at Chiasso with half an hour in hand is ordinary railway, not a gamble. The
#: original instinct survives as the buffer.
MAX_TRANSFERS = 1
MIN_BUFFER = timedelta(minutes=20)


def _change(c: dict) -> tuple[str, timedelta] | None:
    """Where the traveller changes and how long they have. None if direct."""
    sections = [s for s in (c.get("sections") or []) if s.get("journey")]
    if len(sections) < 2:
        return None
    arrive = _iso(sections[0]["arrival"].get("arrival"))
    depart = _iso(sections[1]["departure"].get("departure"))
    if arrive is None or depart is None:
        return None
    return sections[0]["arrival"]["station"]["name"], depart - arrive


def _norm(name: str) -> str:
    """A station name, comparable.

    We ask with "Zurich HB" and the timetable answers "Zürich HB". Both are the
    same platform, and a lookup that treats them as two left every train from
    it filed under whatever default the caller happened to pass -- which is
    fine while the only route is the one in the demo and wrong the moment a
    second city has an umlaut in it. Accents stripped, case and spacing
    flattened, on both sides of the comparison.
    """
    stripped = unicodedata.normalize("NFKD", name or "")
    return " ".join("".join(c for c in stripped
                            if not unicodedata.combining(c)).lower().split())


def offers(origin: str, destination: str, when: datetime, station_map: dict):
    """Wire format -> Offer, with any change named in the label."""
    from offers import Offer

    codes = {_norm(name): code for name, code in (station_map or {}).items()}
    out = []
    for c in connections(origin, destination, when).get("connections", []):
        transfers = int(c.get("transfers", 0))
        if transfers > MAX_TRANSFERS:
            continue

        change = _change(c) if transfers else None
        if transfers and (change is None or change[1] < MIN_BUFFER):
            continue          # unknown or tight connection: not offered at all

        dep = _iso(c["from"]["departure"])
        arr = _iso(c["to"]["arrival"])
        product = (c.get("products") or ["train"])[0]
        via = ""
        if change:
            station, buffer = change
            via = f", change at {station} ({int(buffer.total_seconds() // 60)} min)"
        out.append(Offer(
            id=f"rail-{dep:%H%M}", mode="rail", carrier="SBB",
            label=f"{product} {dep:%H:%M} - {c['from']['station']['name']} "
                  f"to {c['to']['station']['name']}{via}",
            depart=dep, arrive=arr,
            origin=codes.get(_norm(c["from"]["station"]["name"]), "ZRH_HB"),
            destination=codes.get(_norm(c["to"]["station"]["name"]), "MILANO_C"),
            price=ESTIMATE_EUR, price_source="estimate", book_url=DEEP_LINK,
        ))
    return out
