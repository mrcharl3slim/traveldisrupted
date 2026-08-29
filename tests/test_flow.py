"""The loop: search, select, cancel, replan -- on real recorded offers.

Every test here exists because assembling a trip from live data broke something
that a literal had been hiding. The fixtures are committed API responses, so
these run with no keys and no network, and they fail loudly if a re-record moves
the world underneath them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import flow
from builder import assemble, infeasible, onward_after
from domain import Disruption, Kind
from graph import Severity, no_action_model

CEST = timezone(timedelta(hours=2))

#: The day the offers were recorded against. Pinned rather than computed from
#: today so a test that passes this morning still passes in November.
DAY = datetime(2026, 9, 18, 9, 0, tzinfo=CEST)
OUT = DAY + timedelta(days=2)


@pytest.fixture(scope="module")
def parts():
    inbound = flow.search_flights("SIN", "ZRH", DAY)
    first = min(inbound, key=lambda o: o.arrive)
    onward = flow.search_flights("ZRH", "MXP", DAY, after=onward_after(first))
    stays = flow.search_hotels("Milan", "IT", DAY, OUT, code="MXP")
    return first, onward, stays


@pytest.fixture(scope="module")
def trip(parts):
    """The cheapest onward leg that produces a trip somebody could take."""
    first, onward, stays = parts
    for pick in onward:
        built, problems = flow.select([first, pick], stays[:1])
        if not problems:
            return built
    pytest.fail("no feasible selection in the recorded offers")


# --------------------------------------------------------------------------
# assembling
# --------------------------------------------------------------------------


def test_two_purchases_are_two_contracts(trip):
    """Nobody encodes the scenario's central flaw. It falls out of buying two
    tickets in two searches: no airline owes the connection."""
    groups = {b.ticket_group for b in trip.in_order() if b.kind is Kind.FLIGHT}
    assert len(groups) == 2


def test_offer_ids_survive_intact(parts):
    """Duffel ids all begin "off_0000", so a truncated one collides with every
    other one -- and the ticket group is derived from it, which turned two
    separate purchases into a single contract and deleted the premise."""
    first, onward, _ = parts
    assert len({o.id for o in onward}) == len(onward)
    assert first.id.startswith("off_")


def test_an_onward_leg_is_searched_from_when_the_traveller_lands(parts):
    first, onward, _ = parts
    assert onward, "the connection window swallowed every option"
    assert all(o.depart >= onward_after(first) for o in onward)


def test_a_stay_is_placed_where_the_hotel_is(trip):
    """Not where the itinerary implies. Inferring it from the preceding flight
    files a Milan hotel under Zurich the moment the onward leg lands the next
    morning, and every consequence after that is confidently wrong."""
    stay = next(b for b in trip.in_order() if b.kind is Kind.LODGING)
    assert stay.where == "MXP"


def test_a_room_nobody_can_reach_is_refused_at_selection(parts):
    """The EUR 75 bargain that lands at 09:10 tomorrow. A booking flow that
    sells an unreachable room has failed before the engine ever sees it."""
    first, onward, stays = parts
    overnight = [o for o in onward if o.arrive.date() > o.depart.date()]
    if not overnight:
        pytest.skip("no overnight connection in the recording")
    _, problems = flow.select([first, overnight[0]], stays[:1])
    assert problems and "before the room stops being held" in problems[0]


# --------------------------------------------------------------------------
# cancelling
# --------------------------------------------------------------------------


def test_the_gap_is_derived_from_the_leg_that_was_cancelled(trip):
    leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1]
    recovery = flow.replan(trip, leg.id)
    assert recovery.gap is not None
    assert (recovery.gap.origin, recovery.gap.destination) == ("ZRH", "MXP")
    assert recovery.gap.by.hour == 22          # the room, not the check-in desk


def test_cancelling_the_first_leg_strands_the_whole_itinerary(trip):
    """A traveller whose outbound is cancelled has not gone anywhere, so
    nothing later in the trip is reachable -- including the parts that fall on
    the following day.

    The room here is guaranteed until 22:00 in Milan, which is 04:00 the next
    morning where the traveller is standing. That crossed the "later days look
    after themselves" boundary, so EUR 445 of hotel came back "reachable under
    this plan" while the flight that was supposed to deliver them to it was
    critical two lines above. On a longer trip it swallowed everything: a
    23:55 departure settles five minutes after it is cancelled, and the page
    said there was nothing to fix.
    """
    leg = trip.in_order()[0]
    recovery = flow.replan(trip, leg.id)

    downstream = [n for n in recovery.impact.nodes if n.booking.id != leg.id]
    assert downstream, "the fixture trip has nothing after its first leg"
    assert all(n.severity is not Severity.SAFE for n in downstream)
    assert recovery.impact.do_nothing_cost + recovery.impact.at_risk_value > 0
    assert recovery.gap is not None


def test_a_delay_still_lets_the_itinerary_resume_the_next_day(trip):
    """The other half of the same rule, so the fix above cannot be read as
    "cancellations are just delays with a bigger number". A late arrival is
    still an arrival: tomorrow happens, and claiming otherwise would inflate
    every headline in the product."""
    leg = trip.in_order()[0]
    late = Disruption(leg.id, leg.end + timedelta(hours=4), "late inbound", 0.9)

    assert no_action_model(late, trip).settled_from is not None
    tomorrow = late.new_end + timedelta(days=1)
    assert no_action_model(late, trip).presence("MXP", tomorrow) == tomorrow


def test_the_cancelled_flight_is_not_offered_back(trip):
    """The cancellation is ours, so the airline's inventory has not moved and
    the search returns the same JU 0333 at two fares. Re-buying a seat on an
    aircraft that is not going is the one recommendation that cannot be
    defended."""
    leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1]
    recovery = flow.replan(trip, leg.id)

    itself = [o for o in recovery.offers
              if o.depart == leg.start
              and o.label.split(" - ")[0] == leg.title.split(" - ")[0]]
    assert itself, "the recording no longer contains the cancelled flight"
    assert not any(p.id == o.id for o in itself for p in recovery.plans)


def test_a_cancelled_onward_leg_does_not_destroy_the_inbound_fare(trip):
    """The defect this whole pass started from. Walking the itinerary by start
    time picked up the long-haul that landed that morning and charged its
    EUR 1,031 to the cancellation of a EUR 170 hop -- a headline six times too
    large, in the direction that flatters us."""
    inbound, leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][:2]
    recovery = flow.replan(trip, leg.id)
    assert inbound.id not in {n.id for n in recovery.impact.nodes}
    assert recovery.impact.do_nothing_cost < inbound.price


def test_doing_nothing_is_charged_the_same_hotel_as_everything_else(trip):
    """Inaction used to get the room free -- its waste came from the impact
    graph, which had already ruled it defusable -- while every alternative paid
    the full rate for it. Two ledgers, one ranking, and the answer depended on
    which branch computed it."""
    leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1]
    recovery = flow.replan(trip, leg.id)
    stay = next(b for b in trip.in_order() if b.kind is Kind.LODGING)

    stranded = [p for p in recovery.plans
                if p.id == "noop" or stay.id not in p.delivered]
    assert len(stranded) > 1
    assert all(stay.id in p.at_risk_ids for p in stranded)
    assert all(stay.id not in p.wasted_ids for p in stranded)


def test_a_room_that_cannot_be_defused_is_damage(trip):
    """The other half of the same rule. Mitigation is what separates at-risk
    from lost, so a stay without one has to be charged under every plan --
    including inaction."""
    import dataclasses

    from domain import Trip

    bookings = [dataclasses.replace(b, mitigation=None)
                if b.kind is Kind.LODGING else b
                for b in trip.in_order()]
    leg = [b for b in bookings if b.kind is Kind.FLIGHT][1]
    recovery = flow.replan(Trip(bookings), leg.id)
    stay = next(b for b in bookings if b.kind is Kind.LODGING)

    noop = next(p for p in recovery.plans if p.id == "noop")
    assert stay.id in noop.wasted_ids
    assert noop.total_damage >= stay.price


def test_every_plan_is_one_the_traveller_could_board(trip):
    leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1]
    recovery = flow.replan(trip, leg.id)
    for plan in recovery.plans:
        if plan.arrives_at:
            assert plan.arrives_where == "MXP"


def test_the_stay_is_downstream_of_a_flight_that_starts_after_it(trip):
    """Order by start time is not order by consequence. The onward flight
    leaves at 15:10 and the hotel opens its desk at 14:00, so the flight is
    "last" on the clock and the room is still ahead of the traveller. Anything
    that walks an itinerary by start time gets this backwards -- which is how a
    cancelled leg once found nothing to fix while its owner sat in the wrong
    country."""
    stay = next(b for b in trip.in_order() if b.kind is Kind.LODGING)
    leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1]
    assert leg.start > stay.start
    assert flow.replan(trip, leg.id).gap.for_booking == stay.id


def test_doing_nothing_still_makes_the_call_it_is_credited_for(trip):
    """The ledger keeps a room out of the damage column because somebody phones
    the property. A Do nothing plan with an empty action list was claiming that
    rescue without asking anyone to perform it -- the page said "held by a phone
    call" over EUR 443 and, two sections down, "this plan asks nothing of
    anybody"."""
    leg = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1]
    recovery = flow.replan(trip, leg.id)
    noop = next(p for p in recovery.plans if p.id == "noop")

    assert noop.at_risk > 0
    calls = [a for a in noop.actions if a.verb == "notify"]
    assert {a.booking_id for a in calls} == noop.at_risk_ids
    # Free and reversible, so it moves no number and cannot flatter inaction.
    assert noop.net_cash == 0.0
