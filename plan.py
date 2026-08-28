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


def build(trip: Trip, disruption: Disruption, now: datetime,
          baseline: Impact, offer) -> Plan:
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
        return Plan(
            id="noop", name="Do nothing", tagline="", actions=[],
            arrives_at=None, arrives_where=None,
            wasted_ids={n.id for n in baseline.broken},
            wasted=baseline.do_nothing_cost,
        )

    actions: list[Action] = []
    delivered: set[str] = set()
    wasted_ids: set[str] = set()
    wasted = 0.0
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
                    label=f"Email the property about a late check-in",
                    note=b.mitigation or ""))
                delivered.add(b.id)
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
            note=f"departs {offer.depart:%H:%M}, arrives {offer.arrive:%H:%M}"
                 + ("" if getattr(offer, "price_source", "quoted") == "quoted"
                    else " - fare is an ESTIMATE, confirm at checkout")))

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
        tightest=tightest,
    )


def generate(trip: Trip, disruption: Disruption, now: datetime,
             offers) -> list[Plan]:
    """Every option including inaction, ranked by what the disruption costs.

    Doing nothing is scored by the same code as everything else. That is the
    whole argument: it is not a rhetorical baseline, it is a candidate that
    loses.
    """
    baseline = propagate(trip, disruption, now)
    plans = [build(trip, disruption, now, baseline, o) for o in [None, *offers]]
    return sorted(plans, key=lambda p: (p.total_damage,
                                        p.arrives_at or datetime.max.replace(
                                            tzinfo=now.tzinfo)))
