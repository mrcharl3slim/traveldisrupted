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
    described by this, and that is what makes the comparison mean anything:
    `plan.build` runs `propagate` over a model with the offer's arrival added
    and reads the result, so a plan and the inaction it is ranked against are
    judged by the same walk over the same itinerary, differing only in the one
    arrival buying the offer would buy.

    It said "will" here for a long time and never did. Candidates were scored
    by a one-hop test of their own that could see the offer and nothing else --
    not the traveller's surviving legs, not `settled_from` -- so the two halves
    of the ranking disagreed about what was reachable.
    """

    def __init__(self, arrivals: dict[str, datetime],
                 settled_from: datetime | None):
        # Where the traveller demonstrably is, and from when.
        self.arrivals = arrivals
        # Past this moment the disruption has washed out and the itinerary
        # resumes as booked -- you get to tomorrow eventually, whatever
        # tonight looks like.
        #
        # None means it never washes out, and that is not a corner case: it is
        # what a cancellation is. "Tomorrow resumes as booked" is a statement
        # about a traveller who is late, and a traveller whose flight is not
        # going is not late, they are somewhere else. See no_action_model.
        self.settled_from = settled_from

    def presence(self, place: str, by: datetime) -> datetime | None:
        """Earliest the traveller can stand in ``place``. None if never."""
        if self.settled_from is not None and by >= self.settled_from:
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

    def arrive(self, place: str, when: datetime) -> None:
        """Record that the traveller can be in ``place`` from ``when``.

        Earliest wins. Two ways of being in the same city are not two
        arrivals, they are one arrival at whichever comes first, and letting a
        later one overwrite an earlier one would make the traveller harder to
        reach by giving them another way to get there.
        """
        held = self.arrivals.get(place)
        if held is None or when < held:
            self.arrivals[place] = when

    def took(self, b: Booking) -> None:
        """Record that the traveller took ``b`` and is now where it ends.

        Only things that MOVE somebody: a booking with a destination. A hotel
        and a meeting are places you have to be, not journeys that put you
        somewhere, and both carry an origin and no destination -- so this is
        the whole test, and it stays right for the next kind of booking added.
        """
        if b.destination and b.end is not None:
            self.arrive(b.destination, b.end)


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
        #
        # And it never settles. Settling says the itinerary resumes as booked
        # once the day is over, which is true of a delay and false of a
        # cancellation: nothing carries this traveller to the destination
        # overnight, so day two is exactly as out of reach as day one. Letting
        # it settle reported an entire cancelled outbound as harmless — a
        # 23:55 departure settles five minutes later, so the hotel, the
        # meeting and the flight home all came back "reachable under this
        # plan" and the recovery search found nothing to fix.
        where = affected.origin or affected.where
        return ReachModel(arrivals={where: disruption.new_end}, settled_from=None)

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
    #: Whether the traveller can physically be there in time, under whatever
    #: model this walk was run with. NOT a restatement of severity, which is
    #: about money: a EUR 0 dinner that gets missed is SAFE because nothing was
    #: at stake, and a commitment that gets missed is BROKEN with an exposure
    #: of zero. Severity is what it costs; this is what happens.
    attended: bool = False
    #: Time to spare, where there is any. What a plan calls its tightest
    #: connection is the smallest of these.
    slack: timedelta | None = None

    @property
    def id(self) -> str:
        return self.booking.id


@dataclass
class Impact:
    nodes: list[Node]
    now: datetime
    #: Where the traveller can be by the end of this walk, arrivals included.
    #: Kept because the walk is the only thing that knows: `presence` before it
    #: runs sees the disruption point and nothing the traveller does next, and
    #: a caller that asks the question again from scratch is a second
    #: reachability model with all the drift that implies. `plan._can_board`
    #: reads it to ask whether a replacement can be boarded at all.
    reached: ReachModel | None = field(default=None, compare=False)

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
    """Walk the itinerary forward and let the consequences fall out.

    THE WALK CARRIES THE TRAVELLER. Every booking is judged against where the
    traveller can be by then, and a leg they can still take changes that -- so
    taking it is recorded before the next booking is judged. Without it the
    walk was not a walk: each booking was measured against the disruption point
    alone, `transit` knows ground routes and not flights, and a trip that flies
    on somewhere lost every booking past the connection. Zurich to Milan to
    Rome, with the Zurich leg late: the Milan hop is still catchable, it lands
    you in Rome, and the Rome hotel was reported destroyed because no road
    goes there.

    It only ever forgives. A booking is added to the arrivals map when the
    traveller can attend it, which can turn a later BROKEN into SAFE and never
    the other way round -- and it forgives the baseline and every candidate
    plan through the same map, because `plan.build` scores both against the
    broken set this returns.
    """
    model = model or no_action_model(disruption, trip)
    source = trip.by_id(disruption.booking_id)
    nodes = [Node(
        booking=source,
        severity=Severity.SOURCE,
        reason=disruption.reason or "operational delay",
    )]

    # Copied, because walking writes to it. The caller's model describes the
    # disruption and is theirs to reuse -- `plan.generate` builds one per
    # candidate and would otherwise accumulate every previous candidate's
    # itinerary into the next one's.
    reached = ReachModel(dict(model.arrivals), model.settled_from)

    # WALKED BY DEADLINE, NOT BY START. `exposed_to` already argues that
    # downstream is a question about deadlines; it then handed them over in
    # start order, which is a different sequence and the wrong one to walk. The
    # room's desk opens at 14:00 and the onward flight leaves at 15:10, so the
    # hotel came first -- and was judged before the flight that delivers the
    # traveller to it had been taken. A thirty-minute delay, comfortably
    # absorbed by a six-hour connection, reported EUR 445 of room at risk.
    prior: list[str] = [source.id]
    walked: list[Node] = []
    for b in sorted(exposed_to(trip, disruption), key=lambda b: b.must_arrive_by):
        # Taken once and kept, because both facts fall out of it and plan.py
        # needs both: whether the traveller gets there, and by how much.
        at = reached.presence(b.where, b.must_arrive_by)
        attends = at is not None and at <= b.must_arrive_by
        if attends:
            reached.took(b)
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

        walked.append(Node(
            booking=b,
            severity=sev,
            exposure=exposure,
            # Only worth quoting where there is something to save.
            recoverable=recoverable if sev in (Severity.BROKEN, Severity.AT_RISK) else 0.0,
            cutoff=cut.closes if cut else None,
            reason=why,
            parents=list(prior[-1:]),
            attended=attends,
            slack=(b.must_arrive_by - at) if attends else None,
        ))
        prior.append(b.id)

    # Returned in itinerary order, because that is how a person reads a trip.
    # The walk needs consequence order and the page needs the clock; they are
    # different sequences and only one of them is a judgement.
    nodes += sorted(walked, key=lambda n: n.booking.start)
    return Impact(nodes=nodes, now=now, reached=reached)
