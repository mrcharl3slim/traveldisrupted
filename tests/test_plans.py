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


def test_a_plan_that_arrives_too_late_still_loses_the_leg():
    """The other half: forgiveness has to be earned. Same trip, a replacement
    landing after the onward hop has gone."""
    trip, cancelled, now, _ = _flies_on()
    day = datetime(2026, 10, 12, tzinfo=CEST)
    late = Offer("r2", "ZRH", "MXP", day.replace(hour=17),
                 day.replace(hour=18, minute=20), 120.0)

    plan = next(p for p in generate(trip, cancelled, now, [late]) if p.id == "r2")
    assert "l2" in plan.wasted_ids and "l2" not in plan.delivered


def test_rail_plan_is_cheaper_than_the_trip_as_booked(plans):
    """72 bought - 38 taxes back - 48 transfer refund = -14."""
    assert plans["ec317"].net_cash == -14.0


def test_the_other_two_plans_price_out(plans):
    """A: 118 + 18 - 38 = 98.  C: 89 + 18 - 38 = 69.

    The prototype priced the evening flight at +21, by cancelling the Malpensa
    transfer for its EUR 48 refund. The engine keeps it instead: you land at
    Malpensa on that plan, the free-change window is still open at 02:38, so
    moving the pickup to 19:50 costs nothing and you keep a ride you already
    paid for. Cancelling and moving come to the same total damage -- 211 either
    way -- and moving leaves the traveller with more. The prototype picked the
    equal-but-worse branch; nobody noticed, because nothing was computing it.
    """
    assert plans["lx1626"].net_cash == 98.0
    assert plans["lx1902"].net_cash == 69.0
    assert plans["lx1902"].total_damage == 211.0
    assert "transfer" in plans["lx1902"].delivered


def test_doing_nothing_is_scored_by_the_same_code(plans):
    """Not a rhetorical baseline -- a candidate that loses. No cash moves, and
    282 of already-paid value stops existing."""
    noop = plans["noop"]
    assert noop.net_cash == 0.0
    assert noop.wasted == 282.0
    assert noop.total_damage == 282.0


def test_ranking_emerges_rather_than_being_stated(ranked):
    """Nothing tells the engine rail is the answer."""
    assert [p.id for p in ranked] == ["ec317", "lx1902", "lx1626", "noop"]


def test_total_damage_is_the_comparable_number(plans):
    """190 wasted - 86 recovered + 72 spent = 176, against 282 for inaction.

    The headline -14 is a cash flow and the 282 is destroyed value; comparing
    them directly, as the prototype did, mixes units. This is the honest pair.
    """
    assert plans["ec317"].total_damage == 176.0
    assert plans["ec317"].total_damage < plans["noop"].total_damage


def test_only_the_rail_plan_saves_the_museum(plans):
    """Milano Centrale 15:20 + 20 min to the venue clears a 16:00 slot.
    Malpensa 16:35 cannot, on either flight."""
    assert "lastsupper" in plans["ec317"].delivered
    assert plans["ec317"].tightest == ("lastsupper", timedelta(minutes=20))
    for pid in ("lx1626", "lx1902"):
        moved = [a for a in plans[pid].actions if a.booking_id == "lastsupper"]
        assert moved and moved[0].verb == "move" and moved[0].cash_out == 18.0


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
