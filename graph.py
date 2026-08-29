"""What breaks, why, and what it costs -- derived, never asserted.

This is the file that turns a delay into EUR 282. Nothing in it is allowed to
contain a severity or a total that a human typed. If the number is wrong, the
model of the world is wrong, and that is the correct place for the argument to
happen.

THE CENTRAL RELATION. A booking survives a disruption if the traveller can
physically be where it happens before it stops waiting:

    presence(place, day) + transit(place -> booking) <= booking.must_arrive_by

Everything else -- severity, exposure, the total, which nodes light up red in
the demo -- is a consequence of that one inequality applied down the itinerary.

THE OTHER HALF. Being unable to attend something is not the same as losing
money on it. A free-cancellation dinner that gets missed costs nothing; a
slot-locked museum entry costs its full face value. Severity is therefore
exposure-weighted, which is why the graph reports what SURVIVES as confidently
as what fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum

from domain import Booking, Disruption, Trip, transit


class Severity(str, Enum):
    SOURCE = "source"      # the root event itself
    BROKEN = "critical"    # cannot attend, and money dies with it
    AT_RISK = "risk"       # deadline threatened, but defusable
    SAFE = "safe"          # holds, or holds no value to lose


class ReachModel:
    """Where the traveller can be, and when.

    A scenario -- doing nothing, or any candidate recovery plan -- is entirely
    described by this. plan.py will subclass it so every plan is scored by the
    same propagation code that scores inaction, which is the only way the
    comparison means anything.
    """

    def __init__(self, arrivals: dict[str, datetime], settled_from: datetime):
        # Where the traveller demonstrably is, and from when.
        self.arrivals = arrivals
        # Past this moment the disruption has washed out and the itinerary
        # resumes as booked -- you get to tomorrow eventually, whatever
        # tonight looks like.
        self.settled_from = settled_from

    def presence(self, place: str, by: datetime) -> datetime | None:
        """Earliest the traveller can stand in ``place``. None if never."""
        if by >= self.settled_from:
            return by            # later days look after themselves
        best = None
        for held, since in self.arrivals.items():
            leg = transit(held, place)
            if leg is None:
                continue          # no ground route: not somewhere they can be
            arrive = since + leg
            if best is None or arrive < best:
                best = arrive
        return best

    def can_attend(self, b: Booking) -> bool:
        at = self.presence(b.where, b.must_arrive_by)
        return at is not None and at <= b.must_arrive_by


def no_action_model(disruption: Disruption, trip: Trip) -> ReachModel:
    """The traveller sleeps through it and sorts it out tomorrow.

    This is the baseline every plan is measured against, so it has to be
    honest rather than dramatic: they do land in Zurich, they simply take no
    step to rescue the day. Anything on a later date is unaffected -- claiming
    otherwise would inflate the headline number, and an inflated number is the
    first thing a judge will pull on.
    """
    affected = trip.by_id(disruption.booking_id)
    day_end = datetime.combine(
        disruption.new_end.date(), time(23, 59), tzinfo=disruption.new_end.tzinfo)

    if disruption.cancelled:
        # The flight is not going. Doing nothing leaves the traveller at the
        # origin, not late at the destination — so anything reachable only from
        # the destination is not reachable at all, and the plans have to search
        # from where the traveller actually is.
        where = affected.origin or affected.where
        arrivals = {where: disruption.new_end}
    else:
        arrivals = {affected.destination or affected.where: disruption.new_end}

    return ReachModel(arrivals=arrivals, settled_from=day_end + timedelta(minutes=1))


def exposed_to(trip: Trip, disruption: Disruption) -> list[Booking]:
    """The bookings this disruption can still damage, in itinerary order.

    Downstream is a question about DEADLINES, not start times. The first version
    walked everything starting within a day of the event, which is a proxy for
    two different things and gets both wrong. A hotel whose check-in opens at
    14:00 but whose room is held until 22:00 starts before a 15:10 cancellation
    and is very much still at stake; the long-haul that landed at 08:15 that
    morning starts inside the same window and is already flown. Walking by start
    time charged that morning's EUR 1,031 fare to the cancellation of a EUR 170
    onward hop -- a headline six times too large, and wrong in the direction
    that flatters us.

    The boundary is the earlier of the disrupted booking's original end and the
    moment the traveller learns. For a delay that is the original arrival, so
    everything the delay pushes past is in scope. For a cancellation it is the
    moment of learning, so anything still ahead of a traveller standing at the
    origin stays in scope -- including things back at the airport they never
    left.
    """
    source = trip.by_id(disruption.booking_id)
    boundary = min(source.end or source.start, disruption.new_end)
    return [b for b in trip.in_order()
            if b.id != source.id and b.must_arrive_by > boundary]


@dataclass
class Node:
    booking: Booking
    severity: Severity
    exposure: float = 0.0        # lost if nobody does anything
    recoverable: float = 0.0     # clawed back if somebody acts in time
    cutoff: datetime | None = None
    reason: str = ""
    parents: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.booking.id


@dataclass
class Impact:
    nodes: list[Node]
    now: datetime

    def by_id(self, nid: str) -> Node:
        return next(n for n in self.nodes if n.id == nid)

    @property
    def broken(self) -> list[Node]:
        return [n for n in self.nodes if n.severity is Severity.BROKEN]

    @property
    def do_nothing_cost(self) -> float:
        """The headline. Money that evaporates if the traveller is not told."""
        return sum(n.exposure for n in self.broken)

    @property
    def missed(self) -> list[Node]:
        """Commitments that will not happen. Counted, never priced.

        Kept apart from the money on purpose. "EUR 445 and you miss the Milan
        meeting" is two facts a traveller weighs differently, and adding a
        made-up euro value to the second so it can join the first would be the
        engine inventing the most important number on the page.
        """
        return [n for n in self.broken if n.booking.commitment]

    @property
    def at_risk_value(self) -> float:
        """Money a phone call keeps alive. Not damage, and not safe either.

        Reported next to ``do_nothing_cost`` rather than folded into it. Folding
        it in doubles the headline for value nobody has lost yet; leaving it out
        entirely -- which is what happened until a live trip had nothing else in
        it -- prints EUR 0 over a EUR 443 room and looks like the engine has
        stopped working.
        """
        return sum(n.exposure for n in self.nodes if n.severity is Severity.AT_RISK)

    @property
    def act_now_value(self) -> float:
        """What intervening is worth before the first deadline passes."""
        return sum(n.recoverable for n in self.nodes)

    @property
    def next_cutoff(self) -> tuple[datetime, Node] | None:
        live = [(n.cutoff, n) for n in self.nodes if n.cutoff and n.cutoff > self.now]
        return min(live, key=lambda p: p[0]) if live else None


def propagate(trip: Trip, disruption: Disruption, now: datetime,
              model: ReachModel | None = None) -> Impact:
    """Walk the itinerary forward and let the consequences fall out."""
    model = model or no_action_model(disruption, trip)
    source = trip.by_id(disruption.booking_id)
    nodes = [Node(
        booking=source,
        severity=Severity.SOURCE,
        reason=disruption.reason or "operational delay",
    )]

    prior: list[str] = [source.id]
    for b in exposed_to(trip, disruption):
        attends = model.can_attend(b)
        recoverable = b.recoverable_at(now)
        cut = b.policy.next_cutoff(now)

        if attends:
            sev, exposure, why = Severity.SAFE, 0.0, "reachable under this plan"
        elif b.commitment:
            # No fare, no refund, and it is the reason the trip exists. Broken
            # with an exposure of zero: it belongs in the list of things that
            # will not happen, and it must not be added to a euro total, so the
            # headline number stays a number about money.
            sev, exposure, why = (
                Severity.BROKEN, 0.0,
                f"you would miss this{' with ' + b.who if b.who else ''}")
        elif b.price <= 0:
            # Missed, but nothing was ever at stake. Saying so is a feature:
            # a traveller who knows what they can ignore acts faster.
            sev, exposure, why = Severity.SAFE, 0.0, "missed, but nothing prepaid"
        elif b.mitigation:
            sev, exposure, why = Severity.AT_RISK, b.price, b.mitigation
        else:
            sev, exposure, why = Severity.BROKEN, b.price, "cannot be attended"

        nodes.append(Node(
            booking=b,
            severity=sev,
            exposure=exposure,
            # Only worth quoting where there is something to save.
            recoverable=recoverable if sev in (Severity.BROKEN, Severity.AT_RISK) else 0.0,
            cutoff=cut.closes if cut else None,
            reason=why,
            parents=list(prior[-1:]),
        ))
        prior.append(b.id)

    return Impact(nodes=nodes, now=now)
