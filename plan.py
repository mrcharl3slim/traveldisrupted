"""Recovery plans, generated and scored rather than written.

Plan B has to come out cheaper than the trip as booked without anybody telling
it to. If the ranking here needs a thumb on it, the ledger below is wrong and
the pitch is wrong with it.

ONE LEDGER, TWO NUMBERS. Every plan is a set of actions, each with cash out and
cash in, plus a verdict on whether each booking still gets delivered. From that
single ledger:

    net_cash     = cash_out - cash_in          what the traveller pays tonight
    total_damage = wasted + cash_out - cash_in  what the disruption costs, full stop

The first is the number the prototype showed (-EUR 14 for the rail plan). The
second is the only one comparable with the EUR 282 of doing nothing, because
that EUR 282 is destroyed value and the -EUR 14 is a cash flow -- different
units, and the prototype quietly compared them. Both are reported. Neither is
allowed to stand in for the other.

SUNK COSTS. The EUR 142 SWISS fare is spent under every plan including doing
nothing, so it does not belong in net_cash -- only the EUR 38 that acting
claws back does. It does belong in total_damage, because the money is gone.

TODO (day 9-10): a moved timed-entry slot can collide with a later booking --
Plan A's 13 Oct 11:30 museum entry sits on the same day as the 09:15 Como tour.
The prototype flagged this in prose. Detecting it properly needs the same
reachability walk applied to the rewritten itinerary, which is a day's work and
is scheduled, not skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from domain import Booking, Disruption, Kind, Trip, transit
from graph import Impact, ReachModel, Severity, no_action_model, propagate


class Lane(str, Enum):
    """Who does the thing. The honesty of the product lives in this enum."""

    AUTO = "auto"    # Downstream acts: no payment, nothing irreversible
    TAP = "tap"      # deep-linked and pre-filled; the traveller authorises
    CALL = "call"    # no consumer API exists; we hand over a script


# What each provider actually exposes to a consumer application. This table is
# the corrective to the prototype, which promised a "GetYourGuide partner API"
# and a "Welcome Pickups API" in the one-tap lane. Neither is obtainable
# without a signed commercial agreement, so both are deep links like everything
# else a traveller pays for. Stating capability as data rather than as prose is
# what stops the demo over-claiming when nobody is checking.
CAPABILITY: dict[tuple[str, str], Lane] = {
    ("SWISS", "cancel"): Lane.CALL,        # no consumer cancel API for this fare
    ("SWISS", "buy"): Lane.TAP,
    ("SBB", "buy"): Lane.TAP,
    ("Welcome Pickups", "cancel"): Lane.TAP,
    ("Welcome Pickups", "move"): Lane.TAP,
    ("GetYourGuide", "move"): Lane.TAP,
    ("Booking.com", "notify"): Lane.AUTO,  # an email to the property, nothing more
}
DEFAULT_LANE = {"notify": Lane.AUTO, "monitor": Lane.AUTO}


def lane_for(provider: str, verb: str) -> Lane:
    return CAPABILITY.get((provider, verb), DEFAULT_LANE.get(verb, Lane.TAP))


@dataclass
class Action:
    verb: str
    label: str
    lane: Lane
    booking_id: str | None = None
    cash_out: float = 0.0
    cash_in: float = 0.0
    note: str = ""
    deadline: datetime | None = None
    #: "quoted" when a booking API sold us this number, "estimate" when it is
    #: ours because no reachable API quotes the fare. Carried as a field rather
    #: than detected by reading the note: the note is a human sentence, and the
    #: first version of that check looked for "estimate" while the sentence
    #: said "ESTIMATE", so no row was ever flagged and the footnote below the
    #: table claimed a caveat the table never showed.
    price_source: str = ""


@dataclass
class Plan:
    id: str
    name: str
    tagline: str
    actions: list[Action]
    arrives_at: datetime | None
    arrives_where: str | None
    delivered: set[str] = field(default_factory=set)
    wasted_ids: set[str] = field(default_factory=set)
    wasted: float = 0.0
    #: Value that survives only because somebody can be told. Kept out of
    #: ``total_damage`` deliberately: it is not lost, and pretending otherwise
    #: would let every plan claim credit for rescuing it.
    at_risk_ids: set[str] = field(default_factory=set)
    at_risk: float = 0.0
    tightest: tuple[str, timedelta] | None = None

    @property
    def cash_out(self) -> float:
        return sum(a.cash_out for a in self.actions)

    @property
    def cash_in(self) -> float:
        return sum(a.cash_in for a in self.actions)

    @property
    def net_cash(self) -> float:
        """What tonight costs. Negative means the traveller ends up ahead."""
        return self.cash_out - self.cash_in

    @property
    def total_damage(self) -> float:
        """Comparable with doing nothing. This is the number that ranks."""
        return self.wasted + self.net_cash

    def lane(self, lane: Lane) -> list[Action]:
        return [a for a in self.actions if a.lane is lane]


def _reach(disruption: Disruption, trip: Trip, offer) -> ReachModel:
    base = no_action_model(disruption, trip)
    if offer is None:
        return base
    arrivals = dict(base.arrivals)
    arrivals[offer.destination] = offer.arrive
    return ReachModel(arrivals=arrivals, settled_from=base.settled_from)


def _can_reach(offer, b: Booking) -> tuple[bool, timedelta | None]:
    """Can the traveller make ``b`` after taking ``offer``, and by how much?"""
    if offer is None:
        return False, None
    leg = transit(offer.destination, b.where)
    if leg is None:
        return False, None
    at = offer.arrive + leg
    return at <= b.must_arrive_by, b.must_arrive_by - at


def _can_board(disruption: Disruption, trip: Trip, offer) -> bool:
    """Can the traveller actually be standing where this offer departs from?

    Every plan assumed they could. That held while the only disruption was a
    delay, because a delayed flight still lands you at the airport the
    replacement leaves from. A cancellation does not: it leaves you where you
    started, and without this check the engine offers somebody stranded in
    Singapore a train from Zurich — priced, ranked, and recommended.
    """
    if offer is None:
        return True
    base = no_action_model(disruption, trip)
    at = base.presence(offer.origin, offer.depart)
    return at is not None and at <= offer.depart


def _property(b: Booking) -> str:
    """The name on the door, not the channel it was booked through.

    "Tell LiteAPI you are not arriving" is an instruction nobody can follow.
    The provider field is the booking channel and belongs in the lane table; the
    title carries the property, which is who actually has to hear from you.
    """
    return b.title.split(" - ")[0].strip() or b.provider


def _caveat(source: str, quoted: str) -> str:
    """Say which kind of not-quite-a-quote this is.

    Lumping them together was wrong in both directions. A converted fare is a
    real number the airline stated, with a fixed rate applied -- calling it an
    ESTIMATE understates it and invites the traveller to distrust a figure they
    can check. A rail fare genuinely is ours, because no reachable API sells
    Swiss rail tickets, and calling that anything softer than an estimate
    overstates it.
    """
    if source == "converted":
        return (f" - {quoted} converted at a fixed rate" if quoted
                else " - converted from another currency at a fixed rate")
    if source and source != "quoted":
        return " - fare is an ESTIMATE, confirm at checkout"
    return ""


def _designator(label: str) -> str:
    """"JU 0333 - ZRH to MXP via BEG" -> "JU 0333"."""
    return label.split(" - ")[0].strip().upper()


def _is_the_disrupted_flight(b: Booking, offer) -> bool:
    """Is this "replacement" the very flight that just failed?

    It has to be asked because the cancellation is ours, not the airline's. A
    real cancellation empties the seats out of inventory and no search returns
    them. A traveller pressing "my flight was cancelled" tells Duffel nothing,
    so the search comes straight back with the same JU 0333 at two different
    fares -- and the engine, which has no idea it is looking at the flight it
    was asked to escape, prices it, ranks it second, and recommends re-buying a
    seat on an aircraft that is not going.

    Same designator, same airports, same minute. No two distinct flights share
    all three.
    """
    if b.kind is not Kind.FLIGHT or offer is None:
        return False
    return ((b.origin, b.destination) == (offer.origin, offer.destination)
            and b.start == offer.depart
            and _designator(b.title) == _designator(offer.label))


def build(trip: Trip, disruption: Disruption, now: datetime,
          baseline: Impact, offer) -> Plan | None:
    """One candidate, priced.

    Repairs are chosen by booking Kind, never by id -- the engine has no idea
    that "lastsupper" is famous. A transfer is redundant when you do not arrive
    where it starts; a fixed slot is kept when reachable and moved when it is
    not; lodging is defused by telling the property. Those four sentences are
    the entire repair policy.
    """
    if offer is None:
        # Doing nothing means doing nothing. An earlier version let the no-op
        # plan cancel the transfer and move the museum slot, which made
        # inaction look thirty euros better than the trip as booked -- the
        # baseline was quietly rescuing itself. Waste here is taken straight
        # from the impact graph so the two modules cannot drift apart.
        # The one thing inaction still has to do. The ledger calls this value
        # "held by a phone call" -- it is only out of the damage column because
        # somebody makes that call -- so a Do nothing plan with an empty action
        # list was claiming a rescue it had not asked anyone to perform. Free,
        # reversible, and available whether or not you rebook, so it moves no
        # number and changes no ranking: it just stops the page contradicting
        # itself.
        held = [n for n in baseline.nodes
                if n.severity is Severity.AT_RISK and n.booking.mitigation]
        return Plan(
            id="noop", name="Do nothing", tagline="",
            actions=[Action(
                verb="notify", booking_id=n.id,
                lane=lane_for(n.booking.provider, "notify"),
                label=f"Call {_property(n.booking)} before "
                      f"{n.booking.must_arrive_by:%H:%M}",
                note=n.booking.mitigation or "") for n in held],
            arrives_at=None, arrives_where=None,
            wasted_ids={n.id for n in baseline.broken},
            wasted=baseline.do_nothing_cost,
            at_risk_ids={n.id for n in baseline.nodes
                         if n.severity is Severity.AT_RISK},
            at_risk=baseline.at_risk_value,
        )

    if not _can_board(disruption, trip, offer):
        return None          # unreachable: not a worse plan, not a plan at all

    if _is_the_disrupted_flight(trip.by_id(disruption.booking_id), offer):
        return None          # the flight that failed is not its own replacement

    actions: list[Action] = []
    delivered: set[str] = set()
    wasted_ids: set[str] = set()
    wasted = 0.0
    at_risk_ids: set[str] = set()
    at_risk = 0.0
    tightest: tuple[str, timedelta] | None = None

    broken = [n for n in baseline.nodes
              if n.severity in (Severity.BROKEN, Severity.AT_RISK)]

    for node in broken:
        b = node.booking
        recover = b.recoverable_at(now)
        cut = b.policy.next_cutoff(now)

        # --- the leg that was missed -------------------------------------
        if b.kind is Kind.FLIGHT:
            if offer is None:
                wasted_ids.add(b.id); wasted += b.price
                continue
            actions.append(Action(
                verb="cancel", booking_id=b.id, lane=lane_for(b.provider, "cancel"),
                label=f"Cancel {b.title} before {b.must_arrive_by:%H:%M}",
                cash_in=recover, deadline=cut.closes if cut else None,
                note=cut.label if cut else ""))
            wasted_ids.add(b.id); wasted += b.price
            continue

        # --- a transfer you only need if you land where it starts ---------
        if b.kind is Kind.TRANSFER:
            if offer is not None and offer.destination == b.origin:
                actions.append(Action(
                    verb="move", booking_id=b.id, lane=lane_for(b.provider, "move"),
                    label=f"Move the pickup to {offer.arrive + timedelta(minutes=40):%H:%M}",
                    cash_out=0.0, deadline=cut.closes if cut else None,
                    note=cut.label if cut else ""))
                delivered.add(b.id)
            elif recover > 0:
                actions.append(Action(
                    verb="cancel", booking_id=b.id, lane=lane_for(b.provider, "cancel"),
                    label=f"Cancel the transfer for a EUR {recover:,.0f} refund",
                    cash_in=recover, deadline=cut.closes if cut else None,
                    note="not needed on this routing"))
                wasted_ids.add(b.id); wasted += b.price
            else:
                wasted_ids.add(b.id); wasted += b.price
            continue

        # --- lodging: a message, not money --------------------------------
        if b.kind is Kind.LODGING:
            ok, _ = _can_reach(offer, b)
            if ok:
                actions.append(Action(
                    verb="notify", booking_id=b.id, lane=lane_for(b.provider, "notify"),
                    label=f"Email {_property(b)} about a late check-in",
                    note=b.mitigation or ""))
                delivered.add(b.id)
            elif b.mitigation:
                # Unreachable tonight, and still not destroyed: the call that
                # holds the room can be made from anywhere, including from the
                # airport the traveller never left.
                #
                # This branch used to charge the full rate. Doing nothing did
                # not, because its waste comes from the impact graph, which had
                # already ruled the room defusable -- so inaction got the hotel
                # free while every alternative paid EUR 443 for it, and the
                # ranking was comparing two different ledgers. Whatever the
                # right answer is, it cannot depend on which branch of this
                # function computed it.
                actions.append(Action(
                    verb="notify", booking_id=b.id, lane=lane_for(b.provider, "notify"),
                    label=f"Call {_property(b)} before {b.must_arrive_by:%H:%M} — "
                          "this plan does not get you there tonight",
                    note=b.mitigation,
                    deadline=b.must_arrive_by))
                at_risk_ids.add(b.id); at_risk += b.price
            else:
                wasted_ids.add(b.id); wasted += b.price
            continue

        # --- fixed slots: keep if you can get there, move if you cannot ---
        ok, buffer = _can_reach(offer, b)
        if ok:
            delivered.add(b.id)
            if buffer is not None and (tightest is None or buffer < tightest[1]):
                tightest = (b.id, buffer)
            continue
        window = b.policy.best_window(now)
        if window and window.net > 0:
            actions.append(Action(
                verb="move", booking_id=b.id, lane=lane_for(b.provider, "move"),
                label=f"Move {b.title} to a later slot",
                cash_out=window.fee, deadline=window.closes, note=window.label))
            delivered.add(b.id)
        else:
            wasted_ids.add(b.id); wasted += b.price

    if offer is not None:
        actions.insert(1, Action(
            verb="buy", booking_id=None, lane=lane_for(offer.carrier, "buy"),
            label=f"Book {offer.label}, EUR {offer.price:,.0f}",
            cash_out=offer.price,
            price_source=getattr(offer, "price_source", "quoted"),
            note=f"departs {offer.depart:%H:%M}, arrives {offer.arrive:%H:%M}"
                 + _caveat(getattr(offer, "price_source", "quoted"),
                           getattr(offer, "quoted", ""))))

    actions.append(Action(
        verb="monitor", lane=Lane.AUTO,
        label="Recalculate the itinerary and watch every remaining deadline",
        note="re-alerts at T-60 on each cutoff"))

    return Plan(
        id=offer.id if offer else "noop",
        name=offer.label if offer else "Do nothing",
        tagline="", actions=actions,
        arrives_at=offer.arrive if offer else None,
        arrives_where=offer.destination if offer else None,
        delivered=delivered, wasted_ids=wasted_ids, wasted=wasted,
        at_risk_ids=at_risk_ids, at_risk=at_risk,
        tightest=tightest,
    )


def _preference_key(plan: "Plan", preference: str):
    """Order two plans of equal damage by what the traveller said mattered.

    Damage still decides. This only breaks ties -- and ties are common, because
    two flights that both save the same hotel and cost within a euro of each
    other are genuinely equivalent to the arithmetic and not at all equivalent
    to somebody who told us at booking time that they wanted no connections.
    Letting the stated preference decide there is the difference between an
    engine that remembers a person and one that merely computes.

    It is deliberately not allowed to outrank money. A traveller who prefers
    direct flights has not agreed to pay two hundred euros more for one, and an
    engine that quietly assumes they have is doing the thing this whole product
    exists to stop.
    """
    if preference == "fastest" and plan.arrives_at:
        return plan.arrives_at.timestamp()
    if preference == "direct":
        return _stops(plan.name)
    return plan.net_cash


def _stops(label: str) -> int:
    if " via " in label:
        return 1
    tail = label.rsplit(", ", 1)[-1]
    if tail.endswith("stops"):
        try:
            return int(tail.split()[0])
        except ValueError:
            return 0
    return 0


def breaks_preference(plan: "Plan", preference: str) -> bool:
    """True when the best plan is not the kind of trip they asked for.

    Said out loud rather than silently corrected. "This is the only option that
    saves your room, and it has a connection you told me you did not want" is a
    sentence a traveller can act on; quietly demoting it and recommending
    something worse is a decision taken on their behalf.
    """
    return bool(preference == "direct" and plan.arrives_at and _stops(plan.name))


def generate(trip: Trip, disruption: Disruption, now: datetime,
             offers, preference: str = "") -> list[Plan]:
    """Every option including inaction, ranked by what the disruption costs.

    Doing nothing is scored by the same code as everything else. That is the
    whole argument: it is not a rhetorical baseline, it is a candidate that
    loses.

    ``preference`` is whatever the traveller said mattered when they booked.
    It breaks ties and nothing more -- see `_preference_key`.
    """
    baseline = propagate(trip, disruption, now)
    built = [build(trip, disruption, now, baseline, o) for o in [None, *offers]]
    plans = [p for p in built if p is not None]
    latest = datetime.max.replace(tzinfo=now.tzinfo)
    return sorted(plans, key=lambda p: (round(p.total_damage, 2),
                                        _preference_key(p, preference),
                                        p.arrives_at or latest))


@dataclass(frozen=True)
class Gap:
    """The hole the disruption left: where they are, where they must be, by when."""

    origin: str
    destination: str
    not_before: datetime
    by: datetime
    for_booking: str

    @property
    def hours(self) -> float:
        return (self.by - self.not_before).total_seconds() / 3600


def recovery_gap(trip: Trip, disruption: Disruption,
                 now: datetime | None = None) -> Gap | None:
    """What to search for, derived rather than configured.

    The demo searched Zurich to Milan because the scenario said so. That is
    fine until somebody cancels a different leg, and then the engine offers a
    train nobody can reach. Where the traveller stands after the disruption is
    knowable — the origin if the flight is cancelled, the destination if it is
    merely late — and the next place they are contractually due is the first
    booking they can no longer attend. Those two points and a deadline are the
    entire search query.

    Returns None when nothing downstream is out of reach, which is the common
    case and not a failure.
    """
    affected = trip.by_id(disruption.booking_id)
    standing = (affected.origin or affected.where) if disruption.cancelled else (
        affected.destination or affected.where)

    baseline = propagate(trip, disruption, now or disruption.new_end)
    stranded = [
        n for n in baseline.nodes
        # At risk counts as stranded. A hotel the traveller can still reach by
        # getting there is exactly what the search is for; looking only at what
        # is already broken means cancelling the last leg of a journey finds
        # nothing to fix while the traveller sits in the wrong country.
        if n.severity in (Severity.BROKEN, Severity.AT_RISK)
        and n.booking.where and n.booking.where != standing
        # Still ahead, measured by DEADLINE rather than start time. Start time
        # looked equivalent and is not: hotel check-in opens at 14:00 while the
        # room is held until 22:00, so a flight cancelled at 15:10 made the
        # hotel look like something already behind the traveller — and the one
        # place they most need to reach dropped out of the search.
        #
        # A booking genuinely behind them has a deadline in the past, so this
        # one test does both jobs.
        and n.booking.must_arrive_by > disruption.new_end
    ]
    if not stranded:
        return None

    target = min(stranded, key=lambda n: n.booking.must_arrive_by)
    return Gap(
        origin=standing,
        destination=target.booking.where,
        not_before=disruption.new_end,
        by=target.booking.must_arrive_by,
        for_booking=target.id,
    )
