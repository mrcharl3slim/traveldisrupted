"""Cancellation, and the questions it forces that a delay never did.

A delayed flight still lands you at the airport the replacement leaves from, so
for as long as delay was the only disruption the engine could assume the
traveller was wherever the itinerary said. A cancellation breaks that: it leaves
them exactly where they started. Every test here exists because something that
looked correct under delay turned out to be an assumption.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from demo_trip import anchor, build_trip
from domain import Disruption
from graph import Severity, no_action_model, propagate
from offers import build_offers
from plan import generate, recovery_gap

TRIP = build_trip(None)
NOW = anchor(None, 12, 2, 38)
OFFERS = build_offers(None)

DELAYED = Disruption("sq346", anchor(None, 12, 10, 25), "late inbound aircraft", 0.91)
CANCELLED = Disruption("sq346", anchor(None, 12, 2, 0), "cancelled", 1.0, cancelled=True)


def test_a_cancellation_leaves_the_traveller_at_the_origin():
    """Not late at the destination. Different place, different things reachable."""
    model = no_action_model(CANCELLED, TRIP)
    assert "SIN" in model.arrivals
    assert "ZRH" not in model.arrivals


def test_a_delay_still_lands_them_where_they_were_going():
    model = no_action_model(DELAYED, TRIP)
    assert "ZRH" in model.arrivals


def test_a_cancellation_never_settles():
    """"The itinerary resumes as booked once today is over" is a statement
    about a traveller who is late. One whose flight is not going is not late,
    they are in another country, and no amount of tomorrow moves them.

    Letting a cancellation settle was the bug that made a cancelled outbound
    look free: everything downstream fell on a later day, came back "reachable
    under this plan", and the recovery search found nothing to fix.
    """
    assert no_action_model(CANCELLED, TRIP).settled_from is None
    assert no_action_model(DELAYED, TRIP).settled_from is not None


def test_a_cancellation_does_not_reach_the_destination_by_waiting():
    """The rule above, in the terms the graph actually asks it in. There is no
    ground route from Singapore to Milan, and a week of patience does not
    build one."""
    model = no_action_model(CANCELLED, TRIP)
    next_week = CANCELLED.new_end + timedelta(days=7)
    assert model.presence("MXP", next_week) is None
    assert model.presence("SIN", next_week) == CANCELLED.new_end


def test_a_cancellation_has_no_delay_to_report():
    """Modelling it as a very long delay would put an arrival time on a flight
    that is not going anywhere."""
    assert CANCELLED.delay(TRIP) == timedelta(0)
    assert DELAYED.delay(TRIP) > timedelta(0)


def test_no_plan_is_offered_that_the_traveller_cannot_board():
    """The gap this caught. Every offer in the scenario departs Zurich; with the
    Singapore flight cancelled the traveller is in Singapore, and the engine was
    happily pricing and recommending a train they could not reach."""
    plans = generate(TRIP, CANCELLED, NOW, OFFERS)
    assert [p.id for p in plans] == ["noop"]

    still_fine = generate(TRIP, DELAYED, NOW, OFFERS)
    assert len(still_fine) > 1


def test_the_search_query_is_derived_from_where_they_actually_are():
    assert recovery_gap(TRIP, CANCELLED, NOW).origin == "SIN"
    assert recovery_gap(TRIP, DELAYED, NOW).origin == "ZRH"


def test_the_target_is_the_next_thing_still_catchable():
    """The earliest broken deadline is usually the one the disruption has
    already blown past. Searching for a flight that must land before it departs
    returns nothing, slowly."""
    gap = recovery_gap(TRIP, DELAYED, NOW)
    assert gap.by > DELAYED.new_end
    assert gap.hours > 0


def test_the_search_never_points_backwards():
    """Cancelling the second leg once proposed flying back to the first — a
    booking already taken, at a place behind the traveller."""
    cancelled_second = Disruption("lx1608", anchor(None, 12, 8, 20), "cancelled",
                                  1.0, cancelled=True)
    gap = recovery_gap(TRIP, cancelled_second, NOW)
    assert gap.origin == "ZRH" and gap.destination == "MXP"
    assert gap.for_booking != "sq346"


def test_nothing_stranded_means_no_search():
    """Most cancellations late in a trip strand nobody. Returning None is the
    answer, not a failure."""
    late_leg = [b for b in TRIP.in_order() if b.id.startswith("fr")]
    if not late_leg:
        pytest.skip("no late rail leg in this scenario")
    quiet = Disruption(late_leg[-1].id, late_leg[-1].start, "cancelled",
                       1.0, cancelled=True)
    assert recovery_gap(TRIP, quiet, NOW) is None


def test_the_do_nothing_baseline_is_unchanged_by_the_new_field():
    """Regression guard: adding `cancelled` must not move the number the whole
    pitch rests on."""
    assert propagate(TRIP, DELAYED, NOW).do_nothing_cost == 423.0
