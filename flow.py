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

WHERE THE DISRUPTION COMES FROM. Three sources, and the differences are worth
keeping visible. Live status (`aerodatabox`) is the real signal and only reaches
about a week either side of today. A traveller pressing "my flight was
cancelled" is an injected one, and it is not a lesser thing -- it is what
happens when the airline has not published yet and the gate agent has already
said so. "My flight is running late" is the third, and it is a different signal
rather than a smaller version of the second: a delay eventually puts the
traveller at the destination and a cancellation never does, which is the one
distinction the whole engine turns on.

All three produce a Disruption and none is allowed to pretend to be another,
which is why `replan` takes one rather than making one. Building a cancellation
inside it meant a delay could only be answered by lying about what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import duffel
import hotels as hotels_port
import places
import rail as rail_port
from base import PortError
from builder import assemble, infeasible
from domain import Disruption, Kind, Trip
from graph import Impact, propagate
from plan import Gap, Plan, breaks_preference, generate, recovery_gap

#: The hour a rail day starts. The timetable answers with what departs AFTER
#: the time it is asked about, so this is not cosmetic: it decides how much of
#: the day a single search -- and therefore a single recording -- can hold.
#: Six is early enough to catch the first useful train and late enough not to
#: spend the response's limited length on services nobody takes.
RAIL_HOUR = 6

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


def search_rail(origin: str, destination: str, day: datetime) -> list:
    """Trains between two cities, earliest first. Empty rather than raising.

    Takes the same city codes as `search_flights` and resolves the stations
    here, because a traveller asks to go to Milan and the timetable asks about
    Milano Centrale. A place with no station in `places` returns nothing, which
    is the same answer `search_flights` gives for somewhere no airline serves
    and means the same thing: not "this failed", but "there is nothing here".

    ONE API, AND IT IS SWISS. transport.opendata.ch is the only rail source
    this reaches. It routes Swiss stations and the international services that
    run out of them, and it has never claimed more -- so naming a station in
    `places` is not a promise that a train runs to it. The empty list is the
    honest answer for a pair it cannot route, and the caller shows it as
    "no trains found" rather than as a fault.
    """
    start, end = places.by_code(origin), places.by_code(destination)
    if not (start and end and start.station and end.station):
        return []
    if start.station_code == end.station_code:
        return []                    # a train from a city to itself

    # ALWAYS FROM THE TOP OF THE DAY, whatever hour the caller happened to
    # hold. The timetable returns what departs after the moment it is given, so
    # the hour is not cosmetic -- it decides which half of the day exists. It
    # is normalised here rather than asked of callers because the first two
    # callers disagreed: the search endpoint asked at noon and the selection
    # that re-resolves the very same train asked at noon too, while the
    # recording was made at six, and picking the 07:33 service then failed to
    # find it again one request later.
    from_dawn = day.replace(hour=RAIL_HOUR, minute=0, second=0, microsecond=0)
    return sorted(rail_port.offers(start.station, end.station, from_dawn,
                                   places.station_map()),
                  key=lambda o: o.depart)


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


def search_leg(origin: str, destination: str, day: datetime,
               mode: str = "flight", after: datetime | None = None,
               key: str = "") -> list:
    """One leg, whichever way it travels.

    The one place that knows a train is found by asking a timetable and a
    flight by asking Duffel. Every caller that re-resolves a chosen leg needs
    that mapping and none of them should own a copy.

    THE MODE IS A HINT AND THE KEY IS THE TRUTH. Given a key that the named
    mode does not have, the other provider is asked before the option is
    declared gone. "That fare is no longer being sold" is a serious sentence
    and asking the wrong shop is not evidence for it -- which is exactly what a
    client that predates trains produces: it picks the cheapest option, the
    cheapest option is now a train, it names no mode because it has never
    heard of one, and the traveller is told the airline withdrew something no
    airline ever sold.
    """
    def one(m: str) -> list:
        if m == "rail":
            return [o for o in search_rail(origin, destination, day)
                    if after is None or o.depart >= after]
        return search_flights(origin, destination, day, after=after)

    first = one("rail" if mode == "rail" else "flight")
    if not key or any(key in (o.key, o.id) for o in first):
        return first
    other = one("flight" if mode == "rail" else "rail")
    return other if any(key in (o.key, o.id) for o in other) else first


def select(legs: list, hotels: list[dict]) -> tuple[Trip, list[str]]:
    """Chosen offers -> a trip, plus every reason it could not be taken.

    ``legs`` is flights and trains together, in whatever order they happen. The
    parameter was called ``flights`` while that was all it could be; sorting
    them by mode is `builder`'s job and the traveller does not think in modes
    anyway -- they think in "then I go to Milan".

    The problems come back WITH the trip rather than instead of it. A selection
    with an impossible connection is still the selection somebody made, and
    showing them the itinerary next to the reason it does not work is the only
    version of this that teaches anything.
    """
    trip = assemble(legs, hotels)
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
        # Whoever actually operates it. "Cancelled by the airline" was fine
        # while only flights could be cancelled, and reads as a stray copy-paste
        # the first time a traveller sees it under a train.
        reason=f"cancelled by the {'operator' if booking.kind is Kind.RAIL else 'airline'}",
        confidence=1.0,
        cancelled=True,
    )


def delay(trip: Trip, booking_id: str, minutes: int,
          at: datetime | None = None) -> Disruption:
    """The other button. This leg is going, late, and the traveller knows.

    Not a small cancellation, and the engine already insists on the difference:
    a delayed flight eventually puts them at the destination, so everything
    downstream is merely late, and `no_action_model` reads `cancelled` to
    decide which of those two worlds it is in. Injecting a delay is therefore
    the same act as injecting a cancellation and a different signal, which is
    why it gets its own verb rather than a flag on that one.

    ``new_end`` is the new ARRIVAL. That is what the field means for anything
    not cancelled -- `Disruption.delay` subtracts the original arrival from it
    to report how late this is -- and it is emphatically not the moment of
    learning. See `learned_at`.
    """
    booking = trip.by_id(booking_id)
    scheduled = booking.end or booking.start
    return Disruption(
        booking_id=booking_id,
        new_end=scheduled + timedelta(minutes=max(1, minutes)),
        reason=f"delayed {minutes} minutes",
        # The traveller has been told. A live prediction carries the
        # provider's own confidence; a person pressing the button is certain.
        confidence=1.0,
        cancelled=False,
    )


def learned_at(trip: Trip, disruption: Disruption) -> datetime:
    """When the clock starts: the moment the traveller can begin acting.

    For a cancellation this is `new_end`, because that is already what the
    field holds -- there is no arrival to record, so it records the moment of
    learning instead. For a delay the two come apart, and taking `new_end`
    there is badly wrong: it is the new arrival, hours away, so "now" would
    land after deadlines that have not happened yet and the engine would
    report a trip with nothing left to save. You find out at the gate at the
    latest, so the departure bounds it.

    Pessimistic on purpose, like every other clock in this system: it is the
    least time the engine gets to claim it saved.
    """
    return min(disruption.new_end, trip.by_id(disruption.booking_id).start)


def to_dict(disruption: Disruption) -> dict:
    """A disruption that outlives the request that created it.

    It has to be stored, not recomputed. Live status is a query and can be
    asked again; a traveller pressing "cancelled" is an event, and an event
    nobody wrote down did not happen. Without this the watch has nothing to
    watch a minute after the page that started it was closed.
    """
    return {
        "booking_id": disruption.booking_id,
        "new_end": disruption.new_end.isoformat(),
        "reason": disruption.reason,
        "confidence": disruption.confidence,
        "cancelled": disruption.cancelled,
        "injected": True,
    }


def from_dict(raw: dict | None) -> Disruption | None:
    if not raw:
        return None
    try:
        return Disruption(
            booking_id=str(raw["booking_id"]),
            new_end=datetime.fromisoformat(str(raw["new_end"])),
            reason=str(raw.get("reason") or ""),
            confidence=float(raw.get("confidence") or 1.0),
            cancelled=bool(raw.get("cancelled")),
        )
    except (KeyError, TypeError, ValueError):
        # A payload we cannot read is a trip with no disruption, not a crash
        # in a loop that runs every minute for the rest of the deployment.
        return None


def _city(code: str) -> str:
    """The airport code for whatever this place is, station codes included.

    A gap that starts at Milano Centrale is a gap that starts in Milan, and
    Malpensa is fifty minutes away -- which `TRANSIT` already knows, and which
    is the whole reason a traveller stranded off a train can still fly. Without
    this the recovery for a cancelled train searched `search_flights` with
    "MILANO_C", got nothing because that is not an airport, and offered the
    traveller their own baseline.
    """
    place = places.by_code(code)
    return place.code if place else code


def replacements(trip: Trip, disruption: Disruption, gap: Gap | None = None) -> list:
    """Live options for the hole this disruption left, in every mode that fits.

    The query is the gap -- derived in plan.py from where the traveller now
    stands and the next place they are contractually due -- so cancelling a
    different leg searches a different route without anybody editing a config.

    Both modes are asked, because the traveller did not lose a flight or a
    train, they lost a way of being somewhere by a deadline. A cancelled train
    is often best answered by a plane and the reverse is the demo's whole
    point; asking only the mode that broke would rank an empty field.
    """
    gap = gap if gap is not None else recovery_gap(trip, disruption)
    if gap is None:
        return []

    origin, destination = _city(gap.origin), _city(gap.destination)
    found: dict = {}
    for day in range(SEARCH_DAYS):
        when = gap.not_before + timedelta(days=day)
        if when.date() > gap.by.date():
            break
        for mode in (lambda: search_flights(origin, destination, when,
                                            after=gap.not_before),
                     lambda: [o for o in search_rail(origin, destination, when)
                              if o.depart >= gap.not_before]):
            try:
                for offer in mode():
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
    #: The clock every figure above was computed against. Carried rather than
    #: re-derived by the caller: `serve` used to pass `disruption.new_end` to
    #: the page, which is the moment of learning for a cancellation and the new
    #: arrival for a delay -- so the page and the engine would have been
    #: counting down to different things the moment delays became injectable.
    now: datetime | None = None
    preference: str = ""
    #: Set when the recommended plan is not the kind of trip they asked for.
    warning: str = ""

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


def replan(trip: Trip, disruption: Disruption, now: datetime | None = None,
           preference: str = "") -> Recovery:
    """One disruption, answered in full.

    Takes the disruption rather than making one, because there are three ways
    to arrive at this point and the difference between them is worth keeping
    in the caller's hands: live status, a traveller saying their flight is
    cancelled, and a traveller saying it is late. Building a cancellation in
    here meant a delay could only be answered by pretending to be one.

    ``preference`` comes off the stored itinerary, which is where the traveller
    left it when they booked. Asking again at the moment their flight is
    cancelled would be a strange time to take a survey.
    """
    now = now or learned_at(trip, disruption)
    gap = recovery_gap(trip, disruption, now)
    offers = replacements(trip, disruption, gap)
    plans = generate(trip, disruption, now, offers, preference)
    return Recovery(
        disruption=disruption,
        impact=propagate(trip, disruption, now),
        gap=gap,
        offers=offers,
        plans=plans,
        now=now,
        preference=preference,
        warning=("the only plan that saves this trip has a connection, and you "
                 "asked for direct flights"
                 if plans and breaks_preference(plans[0], preference) else ""),
    )
