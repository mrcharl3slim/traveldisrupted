"""Replacement options for the Zurich -> Milan gap.

SEEDED, AND ONLY UNTIL DAY 5-6. These three offers are exactly what
ports/duffel.py and ports/rail.py will return once wired, in the same shape, so
plan.py never learns where they came from. Keeping the seam here means the
switch to live data changes one import and nothing else.

Prices are the ones the prototype asserted; the point of this file is that they
now flow through arithmetic instead of being printed next to a conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import dataclasses
from datetime import date, timedelta

from demo_trip import DEFAULT_BASE, dt


@dataclass(frozen=True)
class Offer:
    id: str
    mode: str            # "flight" | "rail"
    carrier: str
    label: str
    depart: datetime
    arrive: datetime
    origin: str
    destination: str
    price: float
    currency: str = "EUR"
    # "quoted"  -- a real fare from a booking API, safe to put in a total
    # "estimate" -- ours, because no reachable API sells this fare. Shown as an
    # estimate everywhere it appears. Plan B's EUR 72 rail fare is the one that
    # matters: transport.opendata.ch returns timetables, not prices, so the
    # number underneath the headline -EUR 14 is the one figure in the whole
    # engine we cannot quote. Saying so is cheaper than being caught.
    price_source: str = "quoted"
    book_url: str = ""


OFFERS = [
    Offer("lx1626", "flight", "SWISS", "LX 1626 - Zurich to Malpensa",
          dt(12, 15, 30), dt(12, 16, 35), "ZRH", "MXP", 118.0),
    Offer("ec317", "rail", "SBB", "Eurocity 317 - Zurich HB to Milano Centrale",
          dt(12, 11, 33), dt(12, 15, 20), "ZRH_HB", "MILANO_C", 72.0,
          price_source="estimate",
          book_url="https://www.sbb.ch/en/buying/pages/fahrplan/fahrplan.xhtml"),
    Offer("lx1902", "flight", "SWISS", "LX 1902 - Zurich to Malpensa",
          dt(12, 18, 5), dt(12, 19, 10), "ZRH", "MXP", 89.0),
]


def build_offers(base: date | None = None):
    """The same replacement options, under the same calendar shift as the trip.

    Offers and trip must move together or the engine compares a rail departure
    in October with a flight arrival next Tuesday and produces a confident,
    meaningless answer.
    """
    if base is None:
        return OFFERS
    delta = timedelta(days=(base - DEFAULT_BASE).days)
    if not delta:
        return OFFERS
    return [dataclasses.replace(o, depart=o.depart + delta, arrive=o.arrive + delta)
            for o in OFFERS]
