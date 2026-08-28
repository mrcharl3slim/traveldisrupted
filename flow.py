"""Search, select, cancel, replan -- the loop a traveller actually goes round.

Everything before this file answered one question: given a trip and a
disruption, what should you do? That question was always asked of a literal.
This one closes the circle. A trip is assembled from offers somebody chose out
of a live search, a leg is cancelled, and the replacement is searched from the
same providers that sold the original -- so the recovery is priced by the market
rather than by us.

WHAT THIS DELIBERATELY DOES NOT DO. It does not pay. Duffel's test mode and
LiteAPI's sandbox return real schedules and real fares and cannot take money,
which is exactly the right shape for the thing being demonstrated: the hard part
was never the card, it was knowing what to buy and by when. Booking is a lane
(`tap`), and it stays one.

WHERE THE CANCELLATION COMES FROM. Two sources, and the difference is worth
keeping visible. Live status (`aerodatabox`) is the real signal and only reaches
about a week either side of today. A traveller pressing "my flight was
cancelled" is the injected one, and it is not a lesser thing -- it is what
happens when the airline has not published yet and the gate agent has already
said so. Both produce the same Disruption, and neither is allowed to pretend to
be the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import duffel
import hotels as hotels_port
from base import PortError
from builder import assemble, infeasible
from domain import Disruption, Trip
from graph import Impact, propagate
from plan import Gap, Plan, generate, recovery_gap

#: A recovery search asks about the day the traveller is stranded and, when the
#: deadline falls after midnight, the next one too. Any wider and the engine is
#: pricing flights for a day the traveller has no reason to be at the airport.
SEARCH_DAYS = 2


def _is_airport(code: str) -> bool:
    """Can a flight search be pointed at this place at all?

    A three-letter uppercase code is the IATA convention, and a rough test is
    the honest one here: the engine holds place codes that are not airports
    (MILAN, SMG, ZRH_HB) and asking an airline for a flight to a district
    returns nothing, slowly. The alternative -- a hard-coded airport list -- is
    a lie the moment the trip leaves the four cities in the demo.
    """
    return len(code) == 3 and code.isalpha() and code.isupper()


# --------------------------------------------------------------------------
# search and selection
# --------------------------------------------------------------------------


def search_flights(origin: str, destination: str, day: datetime,
                   after: datetime | None = None) -> list:
    """Bookable flights, cheapest first. Empty rather than raising on a miss."""
    if not (_is_airport(origin) and _is_airport(destination)):
        return []
    return sorted(duffel.offers(origin, destination, day, after=after),
                  key=lambda o: o.price)


def search_hotels(city: str, country: str, check_in: datetime,
                  check_out: datetime, limit: int = 5,
                  code: str = "") -> list[dict]:
    """``code`` is the place the search was pointed at -- MXP for Milan.

    Passed in rather than looked up because a city-to-airport table is a
    geocoder in disguise, and a wrong one is worse than none. Left empty, the
    stay falls back to the itinerary, which is a guess -- see builder.assemble.
    """
    return hotels_port.stays(city, country, check_in, check_out,
                             check_in.tzinfo, limit=limit, code=code)


def select(flights: list, hotels: list[dict]) -> tuple[Trip, list[str]]:
    """Chosen offers -> a trip, plus every reason it could not be taken.

    The problems come back WITH the trip rather than instead of it. A selection
    with an impossible connection is still the selection somebody made, and
    showing them the itinerary next to the reason it does not work is the only
    version of this that teaches anything.
    """
    trip = assemble(flights, hotels)
    return trip, infeasible(trip)


# --------------------------------------------------------------------------
# the disruption, and what follows from it
# --------------------------------------------------------------------------


def cancel(trip: Trip, booking_id: str, at: datetime | None = None) -> Disruption:
    """The button. One leg is not going, and the traveller has just found out.

    ``new_end`` is the moment of learning, not an arrival -- there is no
    arrival. Defaulting it to the scheduled departure is the pessimistic honest
    guess: the latest moment somebody could possibly still be in the dark, and
    therefore the least time this engine gets to claim it saved.
    """
    booking = trip.by_id(booking_id)
    return Disruption(
        booking_id=booking_id,
        new_end=at or booking.start,
        reason="cancelled by the airline",
        confidence=1.0,
        cancelled=True,
    )


def replacements(trip: Trip, disruption: Disruption, gap: Gap | None = None) -> list:
    """Live options for the hole this disruption left.

    The query is the gap -- derived in plan.py from where the traveller now
    stands and the next place they are contractually due -- so cancelling a
    different leg searches a different route without anybody editing a config.
    """
    gap = gap if gap is not None else recovery_gap(trip, disruption)
    if gap is None:
        return []

    found: dict = {}
    for day in range(SEARCH_DAYS):
        when = gap.not_before + timedelta(days=day)
        if when.date() > gap.by.date():
            break
        try:
            for offer in search_flights(gap.origin, gap.destination, when,
                                        after=gap.not_before):
                found[offer.id] = offer
        except PortError:
            # No recording for this route and date. A missing fixture is a
            # missing option, not a crash -- the traveller still gets the
            # baseline and every option that did come back.
            continue
    return sorted(found.values(), key=lambda o: o.price)


@dataclass(frozen=True)
class Recovery:
    """One disruption, fully worked: what broke, what to search, what to do."""

    disruption: Disruption
    impact: Impact
    gap: Gap | None
    offers: list
    plans: list

    @property
    def best(self):
        return self.plans[0] if self.plans else None

    @property
    def saved(self) -> float:
        """What acting is worth against not acting. Zero is a real answer."""
        noop = next((p for p in self.plans if p.id == "noop"), None)
        if noop is None or self.best is None:
            return 0.0
        return round(noop.total_damage - self.best.total_damage, 2)


def replan(trip: Trip, booking_id: str, at: datetime | None = None,
           now: datetime | None = None) -> Recovery:
    """Cancel a leg and answer the whole question in one call."""
    disruption = cancel(trip, booking_id, at)
    now = now or disruption.new_end
    gap = recovery_gap(trip, disruption, now)
    offers = replacements(trip, disruption, gap)
    return Recovery(
        disruption=disruption,
        impact=propagate(trip, disruption, now),
        gap=gap,
        offers=offers,
        plans=generate(trip, disruption, now, offers),
    )
