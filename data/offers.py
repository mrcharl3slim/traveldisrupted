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
    currency: str = "SGD"
    # "quoted"  -- a real fare from a booking API, safe to put in a total
    # "estimate" -- ours, because no reachable API sells this fare. Shown as an
    # estimate everywhere it appears. Plan B's EUR 72 rail fare is the one that
    # matters: transport.opendata.ch returns timetables, not prices, so the
    # number underneath the headline -EUR 14 is the one figure in the whole
    # engine we cannot quote. Saying so is cheaper than being caught.
    # "quoted"    a real fare from a booking API, safe to put in a total
    # "estimate"  ours, because no reachable API sells this fare
    # "converted" quoted, but in another currency and turned into this one at a
    #             fixed rate — a real number that has had a guess applied to it
    price_source: str = "quoted"
    book_url: str = ""
    #: What the provider actually said, before any conversion: "USD 767".
    quoted: str = ""
    #: Fare conditions, straight from the airline rather than assumed. A fare
    #: with neither a refund nor a change is genuinely non-refundable, and the
    #: engine treats an absent rule the same way — so reading these turns an
    #: under-promise into the real number.
    refundable: bool = False
    change_fee: float | None = None

    @property
    def key(self) -> str:
        """What this offer IS, rather than the token that points at it.

        Duffel mints offer ids per offer request: search twice for the same
        flight and you get two ids. That is correct on their side -- an id
        names a priced, time-limited contract, not an aircraft -- and it means
        an id is useless for "the thing the traveller pointed at a moment ago".
        Matching on it worked perfectly against recordings, where the fixture
        returns the same ids forever, and failed on every single selection the
        moment the ports went live.

        A designator, two airports and a departure minute survive re-searching
        because they describe the flight. What they deliberately do not carry
        is the fare: the same aircraft is sold at several prices, so a key
        match can return more than one candidate, and choosing between them is
        the caller's job -- see `serve.choose`, which takes the nearest to what
        was shown and reports the difference rather than swallowing it.
        """
        designator = self.label.split(" - ")[0].strip().upper()
        return f"{designator}|{self.origin}|{self.destination}|{self.depart.isoformat()}"


import money as _fx

# The same quotes the prototype carried, crossing the same boundary a live one
# does: EUR at the declared rate, marked converted, the original kept. The
# rail figure was always ours; it stays an estimate, now in home currency.
OFFERS = [
    Offer("lx1626", "flight", "SWISS", "LX 1626 - Zurich to Malpensa",
          dt(12, 15, 30), dt(12, 16, 35), "ZRH", "MXP", _fx.to_home(118.0, "EUR")[0],
          price_source="converted", quoted="EUR 118"),
    Offer("ec317", "rail", "SBB", "Eurocity 317 - Zurich HB to Milano Centrale",
          dt(12, 11, 33), dt(12, 15, 20), "ZRH_HB", "MILANO_C", _fx.to_home(72.0, "EUR")[0],
          price_source="estimate",
          book_url="https://www.sbb.ch/en/buying/pages/fahrplan/fahrplan.xhtml"),
    Offer("lx1902", "flight", "SWISS", "LX 1902 - Zurich to Malpensa",
          dt(12, 18, 5), dt(12, 19, 10), "ZRH", "MXP", _fx.to_home(89.0, "EUR")[0],
          price_source="converted", quoted="EUR 89"),
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
