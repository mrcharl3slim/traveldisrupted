"""The plans must rank themselves.

Every figure below is verifiable by hand from the fare rules and the three
offers. None of them appears as a literal in plan.py.
"""

from datetime import datetime, timedelta

import pytest

from demo_trip import TRIP, CEST, dt
from domain import Booking, Disruption, Kind, Trip
from offers import OFFERS
from plan import Lane, generate

NOW = datetime(2026, 10, 12, 2, 38, tzinfo=CEST)
DISRUPTION = Disruption("sq346", dt(12, 10, 25), "late inbound aircraft", 0.91)


@pytest.fixture
def plans():
    return {p.id: p for p in generate(TRIP, DISRUPTION, NOW, OFFERS)}


@pytest.fixture
def ranked():
    return generate(TRIP, DISRUPTION, NOW, OFFERS)


class Offer:
    """The minimum a plan candidate has to be. Built here rather than taken
    from the recordings because the point is a routing the demo trip does not
    contain: somewhere to fly on to."""

    price_source = "quoted"

    def __init__(self, ident, origin, destination, depart, arrive, price):
        self.id = self.key = self.label = ident
        self.origin, self.destination = origin, destination
        self.depart, self.arrive, self.price = depart, arrive, price
        self.carrier, self.currency = "XX", "EUR"


def _flies_on():
    """ZRH -> MXP -> FCO with a Rome room, and the first leg cancelled."""
    day = datetime(2026, 10, 12, tzinfo=CEST)

    def at(hour, minute=0):
        return day.replace(hour=hour, minute=minute)

    trip = Trip([
        Booking(id="l1", kind=Kind.FLIGHT, provider="LX", title="ZRH to MXP",
                start=at(9), end=at(10, 10), origin="ZRH", destination="MXP",
                price=170.0),
        Booking(id="l2", kind=Kind.FLIGHT, provider="AZ", title="MXP to FCO",
                start=at(16), end=at(17, 20), origin="MXP", destination="FCO",
                price=140.0),
        Booking(id="h1", kind=Kind.LODGING, provider="hotel", title="Rome hotel",
                start=at(14), end=at(23), origin="FCO", price=430.0,
                hard_deadline=at(22),
                mitigation="call the property to hold the room"),
    ])
    cancelled = Disruption("l1", at(8, 30), "cancelled", 1.0, cancelled=True)
    # Lands at 13:40, comfortably ahead of the 16:00 hop they already hold.
    rescue = Offer("r1", "ZRH", "MXP", at(12, 20), at(13, 40), 120.0)
    return trip, cancelled, at(8, 30), [rescue]


def test_a_plan_is_scored_by_the_walk_that_scores_inaction():
    """Candidates had a reachability test of their own -- one ground hop from
    where the offer lands -- while the baseline they are ranked against went
    through `propagate`. The one-hop test cannot see the traveller's own
    surviving legs, so a replacement landing in Milan at 13:40 was still
    charged for the 16:00 flight to Rome it had just saved.

    EUR 140 on every candidate and nothing on doing nothing, which is how the
    engine came to recommend inaction over the plan that rescued the trip."""
    trip, cancelled, now, offers = _flies_on()
    ranked = generate(trip, cancelled, now, offers)
    best, noop = ranked[0], next(p for p in ranked if p.id == "noop")

    assert best.id == "r1", "the rescue has to beat doing nothing"
    assert "l2" in best.delivered and "l2" not in best.wasted_ids
    assert "l2" in noop.wasted_ids, "inaction really does lose that flight"
    assert best.total_damage < noop.total_damage


def test_a_plan_that_keeps_the_meeting_outranks_a_cheaper_one_that_loses_it():
    """Goals before money. The sort was damage-first and a missed commitment
    carried an exposure of zero, so the ranking was money-first and
    meeting-blind: a EUR 120 flight that saved the board meeting lost to a EUR
    95 one that missed it -- and to doing nothing, at EUR 0. The engine
    recommended missing the reason the trip existed to save twenty-five euros.

    The commitment is still never PRICED. It is ranked on, which is different:
    the EUR 25 is then a real number the page shows next to the plan that
    loses the meeting, rather than a weight somebody typed."""
    day = datetime(2026, 10, 12, tzinfo=CEST)

    def at(hour, minute=0):
        return day.replace(hour=hour, minute=minute)

    trip = Trip([
        Booking(id="l1", kind=Kind.FLIGHT, provider="LX", title="ZRH to MXP",
                start=at(9), end=at(10, 10), origin="ZRH", destination="MXP",
                price=170.0),
        Booking(id="m1", kind=Kind.ACTIVITY, provider="calendar",
                title="Board meeting", start=at(14), end=at(15), origin="MILAN",
                price=0.0, commitment=True, who="the board"),
    ])
    cancelled = Disruption("l1", at(8, 30), "cancelled", 1.0, cancelled=True)
    cheap_late = Offer("cheap-late", "ZRH", "MXP", at(15), at(16, 10), 95.0)
    dear_early = Offer("dear-early", "ZRH", "MXP", at(11), at(12, 10), 120.0)

    ranked = generate(trip, cancelled, at(8, 30), [cheap_late, dear_early])
    assert [p.id for p in ranked] == ["dear-early", "noop", "cheap-late"]
    assert ranked[0].saved_ids == {"m1"} and not ranked[0].missed_ids
    assert all("m1" in p.missed_ids for p in ranked[1:])
    # And a missed meeting produces the one action that is possible for it.
    assert any(a.verb == "notify" and a.booking_id == "m1"
               for a in ranked[2].actions)
    assert ranked[0].total_damage == 120.0, "still priced honestly, still ranked first"


def test_the_demo_ranking_is_untouched_by_goal_awareness(ranked):
    """The scripted trip has no commitments, so nothing about its order or its
    figures may move. EUR 282, -EUR 14 and EUR 176 are asserted elsewhere; this
    pins that the sort key's new leading term is zero for every plan."""
    assert all(not p.missed_ids for p in ranked)


def test_a_plan_that_arrives_too_late_still_loses_the_leg():
    """The other half: forgiveness has to be earned. Same trip, a replacement
    landing after the onward hop has gone."""
    trip, cancelled, now, _ = _flies_on()
    day = datetime(2026, 10, 12, tzinfo=CEST)
    late = Offer("r2", "ZRH", "MXP", day.replace(hour=17),
                 day.replace(hour=18, minute=20), 120.0)

    plan = next(p for p in generate(trip, cancelled, now, [late]) if p.id == "r2")
    assert "l2" in plan.wasted_ids and "l2" not in plan.delivered


def test_a_replacement_is_boardable_from_a_leg_they_still_hold():
    """The last place in plan.py with its own idea of where somebody could be.

    `_can_board` built a fresh `no_action_model`, which sees the disruption
    point and nothing the traveller does next -- so an onward option leaving
    Milan was refused to somebody whose delayed long-haul still lands them in
    Zurich in time for the Milan leg they already hold. Refused, not ranked
    low: build returns None, so the option does not appear at all, and the
    only thing on offer was losing the room.
    """
    day = datetime(2026, 10, 12, tzinfo=CEST)

    def at(hour, minute=0):
        return day.replace(hour=hour, minute=minute)

    trip = Trip([
        Booking(id="l1", kind=Kind.FLIGHT, provider="SQ", title="SIN to ZRH",
                start=at(1), end=at(8, 15), origin="SIN", destination="ZRH",
                price=900.0),
        # Still catchable on a 10:25 arrival, and it is what puts them in Milan.
        Booking(id="l2", kind=Kind.FLIGHT, provider="LX", title="ZRH to MXP",
                start=at(15, 10), end=at(16, 30), origin="ZRH", destination="MXP",
                price=170.0),
        Booking(id="h1", kind=Kind.LODGING, provider="hotel", title="Rome hotel",
                start=at(14), end=at(23), origin="FCO", price=430.0,
                hard_deadline=at(22)),
    ])
    late = Disruption("l1", at(10, 25), "late inbound aircraft", 0.91)
    onward = Offer("r1", "MXP", "FCO", at(17, 30), at(18, 50), 95.0)

    ranked = generate(trip, late, at(9), [onward])
    assert [p.id for p in ranked] != ["noop"], "the option was refused outright"

    best, noop = ranked[0], next(p for p in ranked if p.id == "noop")
    assert best.id == "r1" and "h1" in best.delivered
    assert noop.total_damage == 430.0 and best.total_damage == 95.0


def test_a_replacement_they_genuinely_cannot_reach_is_still_refused():
    """The guard this started as, and it has to survive. Stranded in Singapore
    by a cancellation, offered a flight out of Milan -- priced, ranked and
    recommended, until _can_board existed."""
    day = datetime(2026, 10, 12, tzinfo=CEST)

    def at(hour, minute=0):
        return day.replace(hour=hour, minute=minute)

    trip = Trip([
        Booking(id="l1", kind=Kind.FLIGHT, provider="SQ", title="SIN to ZRH",
                start=at(1), end=at(8, 15), origin="SIN", destination="ZRH",
                price=900.0),
        Booking(id="h1", kind=Kind.LODGING, provider="hotel", title="Rome hotel",
                start=at(14), end=at(23), origin="FCO", price=430.0,
                hard_deadline=at(22)),
    ])
    stuck = Disruption("l1", at(0, 30), "cancelled", 1.0, cancelled=True)
    elsewhere = Offer("r9", "MXP", "FCO", at(17, 30), at(18, 50), 95.0)

    assert [p.id for p in generate(trip, stuck, at(0, 30), [elsewhere])] == ["noop"]


def test_rail_plan_is_cheaper_than_the_trip_as_booked(plans):
    """EUR (72 - 38 - 48) = -14 -> S$-21."""
    assert plans["ec317"].net_cash == -21.0


def test_the_other_two_plans_price_out(plans):
    """In EUR: A: 118 + 18 - 38 = 98 -> S$147.  C: 89 + 18 - 38 = 69 -> S$103.50.

    The prototype priced the evening flight at +21, by cancelling the Malpensa
    transfer for its EUR 48 refund. The engine keeps it instead: you land at
    Malpensa on that plan, the free-change window is still open at 02:38, so
    moving the pickup to 19:50 costs nothing and you keep a ride you already
    paid for. Cancelling and moving come to the same total damage -- 211 either
    way -- and moving leaves the traveller with more. The prototype picked the
    equal-but-worse branch; nobody noticed, because nothing was computing it.
    """
    assert plans["lx1626"].net_cash == 147.0
    assert plans["lx1902"].net_cash == 103.5
    assert plans["lx1902"].total_damage == 316.5
    assert "transfer" in plans["lx1902"].delivered


def test_doing_nothing_is_scored_by_the_same_code(plans):
    """Not a rhetorical baseline -- a candidate that loses. No cash moves, and
    S$423 of already-paid value stops existing."""
    noop = plans["noop"]
    assert noop.net_cash == 0.0
    assert noop.wasted == 423.0
    assert noop.total_damage == 423.0


def test_ranking_emerges_rather_than_being_stated(ranked):
    """Nothing tells the engine rail is the answer."""
    assert [p.id for p in ranked] == ["ec317", "lx1902", "lx1626", "noop"]


def test_total_damage_is_the_comparable_number(plans):
    """In EUR: 190 wasted - 86 recovered + 72 spent = 176 -> S$264, against S$423.

    The headline -14 is a cash flow and the 282 is destroyed value; comparing
    them directly, as the prototype did, mixes units. This is the honest pair.
    """
    assert plans["ec317"].total_damage == 264.0
    assert plans["ec317"].total_damage < plans["noop"].total_damage


def test_only_the_rail_plan_saves_the_museum(plans):
    """Milano Centrale 15:20 + 20 min to the venue clears a 16:00 slot.
    Malpensa 16:35 cannot, on either flight."""
    assert "lastsupper" in plans["ec317"].delivered
    assert plans["ec317"].tightest == ("lastsupper", timedelta(minutes=20))
    for pid in ("lx1626", "lx1902"):
        moved = [a for a in plans[pid].actions if a.booking_id == "lastsupper"]
        assert moved and moved[0].verb == "move" and moved[0].cash_out == 27.0


def test_the_hotel_is_defused_by_a_message_under_every_plan(plans):
    for pid in ("ec317", "lx1626", "lx1902"):
        notify = [a for a in plans[pid].actions if a.verb == "notify"]
        assert notify and notify[0].lane is Lane.AUTO
        assert "hotel" in plans[pid].delivered


def test_the_swiss_cancellation_is_the_only_phone_call(plans):
    """The one action no consumer API can perform. If this list ever grows
    silently, the product has started over-claiming."""
    for pid in ("ec317", "lx1626", "lx1902"):
        calls = plans[pid].lane(Lane.CALL)
        assert [a.booking_id for a in calls] == ["lx1608"]
        assert calls[0].deadline == dt(12, 8, 20)


def test_paid_actions_never_land_in_the_automatic_lane(plans):
    """The corrective to the prototype's 'GetYourGuide partner API' claim."""
    for p in plans.values():
        for a in p.lane(Lane.AUTO):
            assert a.cash_out == 0.0 and a.cash_in == 0.0


def test_every_plan_carries_its_offer_s_price_provenance(plans):
    """Regression, and the reason it is worth a test.

    The caveat used to be detected by searching the action's note for
    "estimate" while the note said "ESTIMATE", so no row was ever marked and the
    footnote under the table promised a warning the table never gave. The Swiss
    rail fare is the one number in the whole engine that no reachable API can
    quote; it is the last one that should quietly lose its caveat.

    Keyed off the offers rather than plan names, because the seeded offers and
    the live rail port label the same train differently.
    """
    for offer in OFFERS:
        plan = plans.get(offer.id)
        assert plan is not None, f"no plan built from offer {offer.id}"
        buys = [a for a in plan.actions if a.verb == "buy"]
        assert buys, f"plan {offer.id} books nothing"
        assert all(a.price_source == offer.price_source for a in buys), offer.id

    assert any(o.price_source == "estimate" for o in OFFERS), (
        "the scenario has stopped exercising an estimated fare at all")
