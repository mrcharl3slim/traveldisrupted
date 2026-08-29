"""The nouns. Bookings, trips, and the disruption that hits them.

Deliberately logic-free: everything here is a description of the world, and
every derived judgement -- what breaks, what it costs, what to do instead --
lives in graph.py and plan.py. The split matters because the descriptions come
from an LLM parsing confirmation emails, and the judgements have to be
defensible arithmetic. Keeping them in separate files keeps the boundary
between "the model guessed this" and "we computed this" visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from policy import Policy


class Kind(str, Enum):
    FLIGHT = "flight"
    RAIL = "rail"
    TRANSFER = "transfer"
    LODGING = "lodging"
    ACTIVITY = "activity"


# Ground time between places, in minutes. A real deployment reads these from a
# routing API; hard-coding them here is fine because they are physical facts
# that do not move, unlike prices and schedules. What must NOT be hard-coded is
# any conclusion drawn from them.
TRANSIT: dict[tuple[str, str], int] = {
    ("ZRH", "ZRH_HB"): 12,
    ("MXP", "MILAN"): 50,
    ("MILANO_C", "MILAN"): 15,
    ("MILAN", "SMG"): 28,      # hotel to Santa Maria delle Grazie
    ("MXP", "SMG"): 55,
    ("MILANO_C", "SMG"): 20,

    # Airport to that city's main station, once rail became bookable. Without
    # these a train is a booking the rest of the trip cannot reach: a hotel is
    # filed under the airport code its own search was pointed at, and arriving
    # at Milano Centrale left it as far away as another country. Published
    # transfer times, and the named service is the check.
    ("MXP", "MILANO_C"): 50,                  # Malpensa Express
    ("FLR", "FIRENZE_SMN"): 20,               # T2 tram
    ("FCO", "ROMA_TERMINI"): 32,              # Leonardo Express
    ("CDG", "PARIS_GDL"): 50,                 # RER B, one change
    ("FRA", "FRANKFURT_HBF"): 15,             # S8/S9
    ("MUC", "MUENCHEN_HBF"): 45,              # S1/S8
    ("AMS", "AMSTERDAM_CS"): 17,              # Intercity
    ("BCN", "BARCELONA_SANTS"): 25,           # R2 Nord
    ("MAD", "MADRID_ATOCHA"): 30,             # C1 Cercanías
    ("LHR", "LONDON_STP"): 50,                # Piccadilly, one change
}

# A flight you reach three minutes before pushback is a flight you have missed.
BOARDING_BUFFER = timedelta(minutes=30)
RAIL_BUFFER = timedelta(minutes=10)


def transit(a: str, b: str) -> timedelta | None:
    """Ground time between two places, or None if there is no ground route.

    None is the important return value and it cost us a test to learn why. An
    earlier version answered "six hours" for unknown pairs, meaning to fail
    pessimistically. It does the opposite: six hours from Zurich conjures a
    traveller into Milan by 16:25 with no ticket and no train, which quietly
    marked the EUR 624 hotel as safe. An unknown pair is not a slow connection,
    it is the absence of one, and the engine must say so.
    """
    if a == b:
        return timedelta(0)
    mins = TRANSIT.get((a, b)) or TRANSIT.get((b, a))
    return timedelta(minutes=mins) if mins is not None else None


@dataclass
class Booking:
    """One thing the traveller has paid for.

    ``ticket_group`` is the quiet centre of this whole product. Two flights
    sharing a group are one contract, and the airline owes a reaccommodation
    when the first runs late. Two flights in different groups are two
    contracts, nobody owes anything, and the traveller absorbs the loss. The
    demo trip's entire failure mode is that SQ 346 and LX 1608 do not share
    this string.
    """

    id: str
    kind: Kind
    provider: str
    title: str
    start: datetime
    end: datetime | None = None
    origin: str | None = None
    destination: str | None = None
    price: float = 0.0
    currency: str = "EUR"
    policy: Policy = field(default_factory=Policy)
    ticket_group: str | None = None
    fixed_slot: bool = False
    # A deadline that is later than ``start`` and forgiving until it isn't --
    # a hotel will hold a room past check-in, up to a point.
    hard_deadline: datetime | None = None
    # Set when missing the deadline can be defused by telling somebody, rather
    # than by spending money. This is what separates "at risk" from "lost".
    mitigation: str | None = None
    #: True while this leg is an intention rather than a purchase -- a
    #: replacement the traveller approved but has not yet paid for in the
    #: provider's own checkout. It has to survive storage: a recovery plan that
    #: writes a flight into the itinerary and lets it read as booked has
    #: replaced one silent failure with another, and the traveller finds out at
    #: the gate.
    pending: bool = False
    #: A commitment whose value is not money. A meeting has no fare and no
    #: refund, and missing it still costs the traveller the thing they flew for.
    #:
    #: The engine reads value from `price` everywhere else, which is right for
    #: everything that was bought and wrong for everything that was promised --
    #: a EUR 0 dinner with free cancellation genuinely costs nothing to miss,
    #: and a EUR 0 meeting with the Milan team is the reason the trip exists.
    #: One flag separates them, and it is the only thing that stops an
    #: appointment being filed under "missed, but nothing prepaid".
    commitment: bool = False
    #: Who it is with. Empty for anything bought rather than arranged.
    who: str = ""
    #: Where `price` came from. "quoted" for a fare a provider actually sold,
    #: "estimate" for one that is ours. It travels on the booking rather than
    #: only on the offer because the offer is gone by the time anybody reads
    #: the itinerary, and a Swiss rail fare is the one number in this system no
    #: reachable API quotes -- see ports/rail.py. A total that silently mixes a
    #: quote with a guess is the thing this product exists to catch other
    #: people doing.
    price_source: str = "quoted"

    @property
    def where(self) -> str:
        """Where the traveller must physically be for this to happen."""
        return self.origin or self.destination or "?"

    @property
    def must_arrive_by(self) -> datetime:
        """The real last moment, including any grace the provider allows."""
        if self.hard_deadline:
            return self.hard_deadline
        if self.kind is Kind.FLIGHT:
            return self.start - BOARDING_BUFFER
        if self.kind is Kind.RAIL:
            return self.start - RAIL_BUFFER
        return self.start

    def recoverable_at(self, now: datetime) -> float:
        return self.policy.recoverable_at(now)


@dataclass
class Trip:
    bookings: list[Booking]

    def by_id(self, bid: str) -> Booking:
        for b in self.bookings:
            if b.id == bid:
                return b
        raise KeyError(bid)

    def in_order(self) -> list[Booking]:
        return sorted(self.bookings, key=lambda b: b.start)

    def after(self, when: datetime) -> list[Booking]:
        return [b for b in self.in_order() if b.start > when]


@dataclass
class Disruption:
    """The signal. One booking now ends somewhere other than promised.

    A cancellation is not a very long delay, and modelling it as one gets the
    answer wrong in a way that matters: a delayed flight eventually puts the
    traveller at the destination, so everything downstream is merely late. A
    cancelled flight leaves them exactly where they started, which is a
    different place, with different things reachable from it.

    ``new_end`` on a cancellation is the moment the traveller learns and can
    start acting — not an arrival, because there is no arrival.
    """

    booking_id: str
    new_end: datetime
    reason: str = ""
    confidence: float = 1.0
    cancelled: bool = False

    def delay(self, trip: Trip) -> timedelta:
        if self.cancelled:
            return timedelta(0)          # not late, not going
        original = trip.by_id(self.booking_id).end
        return self.new_end - original if original else timedelta(0)
