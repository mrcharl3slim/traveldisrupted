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


def test_a_cancellation_settles_late_rather_than_never():
    """"The itinerary resumes once today is over" is a statement about a
    traveller who is late. One whose flight is not going is not late, they are
    in another country, and tomorrow morning does not move them. Letting a
    cancellation settle at end of day was the bug that made a cancelled
    outbound look free.

    NEVER settling was the overcorrection, and it cost more than the bug it
    fixed: cancelling the 75-minute ZRH->MXP hop charged S$1,569, of which
    S$1,359 was a Florence hotel and two trains three to five days out --
    unreachable only because `transit("ZRH","FLR")` is not one of fifteen rows
    in a table of local ground links. The horizon is the day after the leg was
    due to land: inside it the traveller is where they are, beyond it we stop
    claiming this disruption owns their week.
    """
    cancelled = no_action_model(CANCELLED, TRIP)
    assert cancelled.settled_from is not None
    # Not tonight, and not tomorrow either -- the two the bug got wrong.
    assert cancelled.settled_from > CANCELLED.new_end + timedelta(days=1)
    # A delay still resumes the same evening; nothing about that changed.
    assert no_action_model(DELAYED, TRIP).settled_from < DELAYED.new_end + timedelta(days=1)


def test_a_cancellation_does_not_reach_the_destination_by_waiting_a_night():
    """The rule above, in the terms the graph asks it in.

    The earlier version of this test asserted the same thing a week out, on the
    grounds that "there is no ground route from Singapore to Milan, and a week
    of patience does not build one". True, and it proves too much: a week of
    patience buys another flight, which is precisely what every recovery plan
    on offer does. What the engine can honestly say is that nobody crosses that
    distance tonight or tomorrow -- and that beyond the horizon its silence is
    ignorance, not a claim of destruction.
    """
    model = no_action_model(CANCELLED, TRIP)
    tomorrow = CANCELLED.new_end + timedelta(hours=20)
    assert model.presence("MXP", tomorrow) is None
    assert model.presence("SIN", tomorrow) == CANCELLED.new_end


def test_a_short_hop_does_not_destroy_the_far_end_of_the_trip():
    """The regression the horizon exists for, in money.

    A cancelled 75-minute hop must not charge a hotel four days later in
    another city. The same-day damage is real and stays; what leaves is the
    part that was never a fact about the traveller.
    """
    trip = build_trip(None)
    hop = Disruption("lx1608", anchor(None, 12, 5, 0), "cancelled", 1.0, cancelled=True)
    impact = propagate(trip, hop, anchor(None, 12, 2, 38))

    charged = {n.id for n in impact.nodes if n.exposure}
    assert "palazzo" not in charged, "a Florence hotel on 15 Oct is not this hop's doing"
    assert "fr9520" not in charged and "fr9508" not in charged
    assert {"transfer", "lastsupper"} <= charged, "the same-day damage is real"
    assert impact.do_nothing_cost < 1000


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
