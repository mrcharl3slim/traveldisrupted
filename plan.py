"""Recovery plans, generated and scored rather than written.

Plan B has to come out cheaper than the trip as booked without anybody telling
it to. If the ranking here needs a thumb on it, the ledger below is wrong and
the pitch is wrong with it.

ONE LEDGER, TWO NUMBERS. Every plan is a set of actions, each with cash out and
cash in, plus a verdict on whether each booking still gets delivered. From that
single ledger:

    net_cash     = cash_out - cash_in          what the traveller pays tonight
    total_damage = wasted + cash_out - cash_in  what the disruption costs, full stop

The first is the number the prototype showed (-S$21 for the rail plan). The
second is the only one comparable with the S$423 of doing nothing, because
that S$423 is destroyed value and the -S$21 is a cash flow -- different
units, and the prototype quietly compared them. Both are reported. Neither is
allowed to stand in for the other.

SUNK COSTS. The SWISS fare (EUR 142, S$213 at the declared rate) is spent under
every plan including doing nothing, so it does not belong in net_cash -- only
the S$57 of taxes that acting
claws back does. It does belong in total_damage, because the money is gone.

KNOWN AND OPEN: a moved timed-entry slot can collide with a later booking --
Plan A's 13 Oct 11:30 museum entry sits on the same day as the 09:15 Como tour.
The prototype flagged this in prose and the engine still does not: `build`
prices the move without walking the rewritten itinerary again.

`builder.clashes` already computes precisely this, and is wired only into the
appointment path. Closing it means running it over the trip a plan WOULD
produce and letting the count rank alongside missed commitments -- the same
lexicographic key `generate` already sorts on. That is a day's work, not a
rewrite, and it is stated in README section 5 rather than left in this
docstring for somebody to find.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from domain import Booking, Disruption, Kind, Trip
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
    #: Who this is with -- the carrier for a purchase, the booking's provider
    #: for everything else. The permission engine reads it to honour "never
    #: Ryanair", and it has to be on the action because by the time a plan is
    #: judged the offer that named the carrier is gone.
    provider: str = ""

    @property
    def id(self) -> str:
        """What a traveller points at when they say "not that one".

        Verb and booking, because that pair is unique within a plan -- a
        booking gets one repair -- and stable across re-searching, which a
        label is not: the label carries a fare or a time that the second
        search may quote differently.
        """
        return f"{self.verb}:{self.booking_id or 'offer'}"


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

    #: The commitments -- meetings, the reason the trip exists -- this plan
    #: keeps and the ones it loses. Counted, never priced, exactly as the
    #: impact graph counts them; but counted HERE too, because they rank.
    #: See `generate`.
    saved_ids: set[str] = field(default_factory=set)
    missed_ids: set[str] = field(default_factory=set)
    #: "flight" or "rail" for a plan that buys something, "" for inaction.
    #: What the permission engine's "never rail" is a statement about.
    mode: str = ""

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

    #: The offer this plan books, identified by what it is rather than by an
    #: id that expires with the search that produced it. Empty for `noop`,
    #: which books nothing and therefore always exists.
    key: str = ""

    def lane(self, lane: Lane) -> list[Action]:
        return [a for a in self.actions if a.lane is lane]


def _reach(disruption: Disruption, trip: Trip, offer) -> ReachModel:
    """Where the traveller can be if they take ``offer``. None means inaction.

    One extra arrival on top of the no-action model, which is the entire
    difference between a plan and doing nothing.
    """
    base = no_action_model(disruption, trip)
    if offer is None:
        return base
    under = ReachModel(dict(base.arrivals), base.settled_from)
    under.arrive(offer.destination, offer.arrive)
    return under


def _outcomes(trip: Trip, disruption: Disruption, now: datetime, offer) -> dict:
    """Every booking under this plan, judged by the code that judges inaction.

    This function is the promise `ReachModel` has always made and did not
    keep. Candidates were scored by a one-hop test of their own -- ground
    transit from the offer's landing point to the booking, and nothing else --
    while the baseline they are compared against went through `propagate`.
    Two reachability models, one ranking, and the answer depended on which one
    happened to run.

    The one-hop test could only ever see the offer. It could not see the
    traveller's own surviving legs, so a plan that lands them in Milan in time
    for the flight to Rome they already hold was still charged for everything
    in Rome -- the same blindness `propagate` was fixed for, left behind in the
    half of the engine that decides what to recommend. It could not see
    `settled_from` either, so it disagreed with the baseline about tomorrow.

    Now both sides walk the same itinerary with the same rules and differ only
    in the one arrival the offer adds.
    """
    walked = propagate(trip, disruption, now, _reach(disruption, trip, offer))
    return {n.booking.id: n for n in walked.nodes}


def _can_board(baseline: Impact, offer) -> bool:
    """Can the traveller actually be standing where this offer departs from?

    Every plan assumed they could. That held while the only disruption was a
    delay, because a delayed flight still lands you at the airport the
    replacement leaves from. A cancellation does not: it leaves you where you
    started, and without this check the engine offers somebody stranded in
    Singapore a train from Zurich — priced, ranked, and recommended.

    Asked of the baseline walk rather than of a fresh `no_action_model`, which
    is the last place in this file that had its own idea of where somebody
    could be. The difference is the traveller's own surviving legs: a leg they
    can still take puts them somewhere, and a replacement leaving from there is
    boardable. Building the model here could not see that, so it refused the
    onward options of anybody whose recovery began with a flight they already
    held.
    """
    if offer is None:
        return True
    reached = baseline.reached
    if reached is None:
        return True                # a walk that kept no map cannot refuse one
    at = reached.presence(offer.origin, offer.depart)
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
            id="noop", name="Do nothing", tagline="", key="noop",
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
            saved_ids={n.id for n in baseline.nodes
                       if n.booking.commitment and n.attended},
            missed_ids={n.id for n in baseline.missed},
        )

    if not _can_board(baseline, offer):
        return None          # unreachable: not a worse plan, not a plan at all

    if _is_the_disrupted_flight(trip.by_id(disruption.booking_id), offer):
        return None          # the flight that failed is not its own replacement

    under = _outcomes(trip, disruption, now, offer)

    actions: list[Action] = []
    delivered: set[str] = set()
    wasted_ids: set[str] = set()
    wasted = 0.0
    at_risk_ids: set[str] = set()
    at_risk = 0.0
    #: Bookings the impact graph judged defusable rather than destroyed -- a
    #: room held past check-in for the price of a phone call. `noop` takes its
    #: waste straight from `Impact.do_nothing_cost`, which counts BROKEN only,
    #: so anything in here is free to inaction by construction.
    defusable = {n.id for n in baseline.nodes if n.severity is Severity.AT_RISK}

    def write_off(b: Booking) -> None:
        """Book an undelivered booking to the column the baseline used.

        THE BUG THIS EXISTS TO END. The lodging branch below already carries a
        long comment explaining that charging an AT_RISK room at full price
        made inaction cheaper than every alternative, because the two were
        priced in different ledgers. That reasoning was written out, agreed
        with, and then applied to `Kind.LODGING` alone -- transfers and fixed
        slots went on charging full price for exactly the bookings `noop` got
        for nothing.

        The consequence is not a rounding error: with a defusable S$300 tour
        that no plan reaches, every rescue was charged S$300 for an outcome
        identical to doing nothing, and `generate` ranked DOING NOTHING FIRST.
        A disruption engine that recommends inaction because of a ledger
        mismatch has failed at the only thing it does.

        As that comment says: whatever the right answer is, it cannot depend on
        which branch of this function computed it. So it is decided in one
        place, for every kind, by what the impact graph already concluded.
        """
        nonlocal wasted, at_risk
        if b.id in defusable:
            at_risk_ids.add(b.id); at_risk += b.price
        else:
            wasted_ids.add(b.id); wasted += b.price

    tightest: tuple[str, timedelta] | None = None
    # Commitments this plan keeps, including the ones the disruption never
    # threatened: a plan is judged on the whole trip's goals, not only on the
    # ones that were at stake, or a plan that saves one meeting by losing
    # another would look like a rescue.
    saved_ids: set[str] = {n.booking.id for n in under.values()
                           if n.booking.commitment and n.attended}
    missed_ids: set[str] = set()

    broken = [n for n in baseline.nodes
              if n.severity in (Severity.BROKEN, Severity.AT_RISK)]

    for node in broken:
        b = node.booking
        recover = b.recoverable_at(now)
        cut = b.policy.next_cutoff(now)

        # --- the leg that was missed -------------------------------------
        if b.kind is Kind.FLIGHT:
            # Unless this plan gets them to it. A replacement that lands in
            # Milan by 13:40 has saved the 16:00 hop to Rome, and cancelling
            # it anyway charged the traveller its full fare under every plan
            # and none under doing nothing -- so the engine recommended
            # inaction over the option that rescued the trip. The flight
            # branch was the last one deciding this without asking the walk.
            reached = under.get(b.id)
            if reached is not None and reached.attended:
                delivered.add(b.id)
                buffer = reached.slack
                if buffer is not None and (tightest is None or buffer < tightest[1]):
                    tightest = (b.id, buffer)
                continue
            if offer is None:
                write_off(b)
                continue
            actions.append(Action(
                verb="cancel", booking_id=b.id, lane=lane_for(b.provider, "cancel"),
                label=f"Cancel {b.title} before {b.must_arrive_by:%H:%M}",
                cash_in=recover, deadline=cut.closes if cut else None,
                note=cut.label if cut else ""))
            write_off(b)
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
                    label=f"Cancel the transfer for a S${recover:,.0f} refund",
                    cash_in=recover, deadline=cut.closes if cut else None,
                    note="not needed on this routing"))
                write_off(b)
            else:
                write_off(b)
            continue

        # --- lodging: a message, not money --------------------------------
        if b.kind is Kind.LODGING:
            reached = under.get(b.id)
            if reached is not None and reached.attended:
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
                write_off(b)
            continue

        # --- fixed slots: keep if you can get there, move if you cannot ---
        reached = under.get(b.id)
        if reached is not None and reached.attended:
            delivered.add(b.id)
            buffer = reached.slack
            if buffer is not None and (tightest is None or buffer < tightest[1]):
                tightest = (b.id, buffer)
            continue

        # --- a commitment this plan does not reach ------------------------
        # No fare to waste and no slot to move: the one thing that can be done
        # is to tell the person, and that is a thing this system can genuinely
        # do. It goes in `missed_ids` rather than `wasted_ids` because it is
        # not money, and it is what `generate` ranks on before money.
        if b.commitment:
            missed_ids.add(b.id)
            actions.append(Action(
                verb="notify", booking_id=b.id,
                lane=lane_for(b.provider, "notify"), provider=b.provider,
                label=f"Tell {b.who or 'them'} you will miss {b.title}",
                note="this plan does not get you there in time"))
            continue
        window = b.policy.best_window(now)
        if window and window.net > 0:
            actions.append(Action(
                verb="move", booking_id=b.id, lane=lane_for(b.provider, "move"),
                label=f"Move {b.title} to a later slot",
                cash_out=window.fee, deadline=window.closes, note=window.label))
            delivered.add(b.id)
        else:
            write_off(b)

    if offer is not None:
        actions.insert(1, Action(
            verb="buy", booking_id=None, lane=lane_for(offer.carrier, "buy"),
            provider=offer.carrier,
            label=f"Book {offer.label}, S${offer.price:,.0f}",
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
        saved_ids=saved_ids, missed_ids=missed_ids,
        mode=getattr(offer, "mode", "flight") if offer is not None else "",
        key=getattr(offer, "key", "") if offer is not None else "",
    )


def abandon(trip: Trip, now: datetime, only: set[str] | None = None) -> Plan:
    """The traveller is not going. What has to happen to each booking, and who
    can do it.

    ``only`` scopes the decision to some of the bookings -- dropping the
    flights out of a trip whose hotel and meetings stand. The arithmetic is
    identical either way: one action per booking in scope, the refund the
    policy still returns at ``now``, and the rest in the waste column. What it
    is NOT is a partial abandonment of the trip: the trip goes on, which is
    why the caller stores the outcome somewhere other than `abandoned`.

    NOT A DISRUPTION, and the difference is the whole reason this is its own
    function rather than a `Disruption` with a flag. Nothing broke; the
    traveller changed their mind. There is no reachability to walk, no gap to
    search and no alternative to rank -- every other plan in this file answers
    "what instead?" and this one answers "what now?". Reusing `build` would
    have meant inventing a disruption that did not happen so the machinery
    would run, which is the engine lying to itself to reach a familiar shape.

    What it does share is the part worth sharing: one `Action` per booking, in
    the lane that can actually perform it, with the money and the deadline the
    policy already knows. `act.perform` then runs it exactly as it runs a
    recovery, because from there the question -- who does this and did they --
    is identical.

    ONE ACTION EACH, and the ledger falls out of them. `cash_in` is what the
    policy still returns at ``now``, so cancelling on Tuesday and cancelling on
    Friday are different numbers and the page can say which. What is not
    recovered is `wasted`, so `total_damage` is what abandoning the trip costs
    -- the same arithmetic that prices a recovery, asked about a decision
    instead of an accident.
    """
    actions: list[Action] = []
    wasted_ids: set[str] = set()
    wasted = 0.0

    for b in trip.in_order():
        if only is not None and b.id not in only:
            continue
        # Nothing was bought, so there is nothing to cancel. A replacement the
        # traveller approved and never paid for leaves with the trip, and
        # listing it as an errand would send somebody to a provider that has
        # never heard of them.
        if b.pending:
            actions.append(Action(
                verb="drop", booking_id=b.id, lane=Lane.AUTO,
                label=f"Drop {b.title}",
                note="never purchased — nothing to cancel"))
            continue

        # A promise, not a purchase. There is no provider to cancel with and no
        # money to get back; the whole of the act is telling the person, which
        # is the one thing here that can genuinely be done for them.
        if b.commitment:
            actions.append(Action(
                verb="notify", booking_id=b.id,
                lane=lane_for(b.provider, "notify"),
                label=f"Tell {b.who or 'them'} you are not coming"
                      + (f" — {b.title}" if b.title else ""),
                note="no fare, nothing to refund"))
            continue

        recover = b.recoverable_at(now)
        cut = b.policy.next_cutoff(now)
        lost = round(max(0.0, b.price - recover), 2)

        # A room with nothing left to recover. Sending somebody to a booking
        # portal to press cancel on a non-refundable rate is an errand that
        # returns nothing; the act that still matters is telling the property,
        # and unlike the portal it is one this system can actually perform.
        # Where the rate IS still worth something the portal is the right
        # place, because that is where the money is.
        if b.kind is Kind.LODGING and not recover and b.mitigation:
            wasted_ids.add(b.id)
            wasted += b.price
            actions.append(Action(
                verb="notify", booking_id=b.id,
                lane=lane_for(b.provider, "notify"),
                label=f"Tell {_property(b)} you are not coming",
                price_source=b.price_source,
                note=f"S${b.price:,.0f} is not refundable — this "
                     "releases the room, it does not recover the rate"))
            continue
        # The FULL fare goes in the waste column and the refund offsets it in
        # `cash_in`, which is how `build` keeps its books -- `total_damage`
        # subtracts one from the other. Booking the net figure here instead
        # took the refund off twice and reported a trip costing EUR 1,516 to
        # abandon that actually costs EUR 2,234.
        if b.price:
            wasted_ids.add(b.id)
            wasted += b.price

        actions.append(Action(
            verb="cancel", booking_id=b.id,
            lane=lane_for(b.provider, "cancel"),
            label=f"Cancel {b.title}",
            cash_in=recover,
            deadline=cut.closes if cut else None,
            price_source=b.price_source,
            note=(cut.label if cut else "")
                 or (f"S${lost:,.0f} is not refundable" if lost
                     else "nothing was at stake")))

    return Plan(
        id="abandon", name="Cancel the whole trip", tagline="", key="abandon",
        actions=actions,
        arrives_at=None, arrives_where=None,
        wasted_ids=wasted_ids, wasted=round(wasted, 2),
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

    GOALS BEFORE MONEY. A plan that keeps the meeting outranks every plan that
    loses it, whatever they cost. Until this, the sort was damage-first and a
    missed commitment carried an exposure of zero, so the ranking was
    money-first and meeting-blind: a S$180 flight that saved the board
    meeting lost to a S$143 one that missed it, and the engine recommended
    missing the reason the trip existed to save twenty-five euros.

    The principle that a commitment is never PRICED still holds -- there is no
    euro figure for a meeting anywhere in this file. It is ranked on, which is
    different: lexicographic, count of missed commitments first, then damage.
    The cost of keeping the meeting is then a real number the page shows next
    to the cheaper plan that loses it, rather than a weight somebody typed.

    ``preference`` is whatever the traveller said mattered when they booked.
    It breaks ties in damage and nothing more -- see `_preference_key`.
    """
    baseline = propagate(trip, disruption, now)
    built = [build(trip, disruption, now, baseline, o) for o in [None, *offers]]
    plans = [p for p in built if p is not None]
    latest = datetime.max.replace(tzinfo=now.tzinfo)

    # STRANDED BEATS CHEAP. When the disruption leaves the traveller short of
    # somewhere they are contractually due -- `recovery_gap` returns None
    # unless it does -- a plan that gets them there outranks one that does
    # not, whatever it costs. Without this, cancelling a long-haul was
    # answered with "do nothing": the S$240 of downstream bookings it wastes
    # is genuinely cheaper than any S$555 replacement, so inaction won the
    # arithmetic and the traveller was advised not to take their trip. The
    # ledger was right and the recommendation was absurd, because the ledger
    # prices what is lost and not the journey itself.
    #
    # Doing nothing stays in the list with its real damage beside it -- it is
    # still an option and the page still shows what it costs. It just stops
    # being the recommendation while somewhere the traveller must be is out
    # of reach. Where nothing is stranded, the old order is untouched: a
    # thirty-minute delay the connection absorbs still recommends inaction,
    # and that restraint is the point.
    gap = recovery_gap(trip, disruption, now)

    def strands(p) -> int:
        if gap is None:
            return 0
        closes = (gap.for_booking in p.delivered
                  or (p.arrives_where and p.arrives_where == gap.destination))
        return 0 if closes else 1

    return sorted(plans, key=lambda p: (len(p.missed_ids),
                                        strands(p),
                                        round(p.total_damage, 2),
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
