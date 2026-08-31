"""The tests that make the demo defensible.

A judge's fair question is "did you compute EUR 282 or type it?". These are the
answer. Every expected value below is one a human can verify from the fare
rules by hand, and none of them appears as a literal anywhere in the engine.
"""

from datetime import datetime, timedelta, timezone

import pytest

from demo_trip import TRIP, CEST, dt
from domain import Booking, Disruption, Kind, Trip
from graph import Severity, propagate

# Detection lands at 02:38 on the 12th -- 5 h 42 m before the SWISS cutoff,
# which is what the countdown in the UI is anchored to.
NOW = datetime(2026, 10, 12, 2, 38, tzinfo=CEST)

DISRUPTION = Disruption(
    booking_id="sq346",
    new_end=dt(12, 10, 25),
    reason="late inbound aircraft",
    confidence=0.91,
)


@pytest.fixture
def impact():
    return propagate(TRIP, DISRUPTION, NOW)


def test_delay_is_derived_not_stated():
    assert DISRUPTION.delay(TRIP) == timedelta(hours=3, minutes=50)


def test_the_headline_number(impact):
    """EUR (142 + 48 + 92) = EUR 282, at the declared 1.50: S$423. If this
    drifts, the pitch is wrong, not the test."""
    assert impact.do_nothing_cost == 423.0
    assert {n.id for n in impact.broken} == {"lx1608", "transfer", "lastsupper"}


def test_acting_is_worth_less_than_doing_nothing_costs(impact):
    """EUR 38 taxes + 48 transfer + (92 - 18) date change = EUR 160 -> S$240.

    Worth stating explicitly: recovery never returns the full 282. Selling
    otherwise would be a lie the fare rules do not support.
    """
    assert impact.act_now_value == 240.0


def test_missed_connection_is_reachability_not_a_flag(impact):
    """Nothing marks LX 1608 critical. 10:25 + 30 min boarding > 08:20 does."""
    assert impact.by_id("lx1608").severity is Severity.BROKEN
    assert impact.by_id("lx1608").recoverable == 57.0


def test_transfer_window_closes_while_the_traveller_is_airborne(impact):
    """The quiet EUR 48. Its deadline is four hours before a pickup nobody
    will make, and it passes at 06:00 -- mid-flight, hours before landing."""
    node = impact.by_id("transfer")
    assert node.severity is Severity.BROKEN
    assert node.cutoff == dt(12, 6, 0)
    assert node.cutoff < DISRUPTION.new_end


def test_hotel_is_at_risk_not_lost(impact):
    """A message defuses it, so it must never be counted in the S$423."""
    node = impact.by_id("hotel")
    assert node.severity is Severity.AT_RISK
    assert node.exposure == 936.0
    assert node.id not in {n.id for n in impact.broken}


def test_engine_reports_what_survives(impact):
    """Free-cancellation dinner is missed but costs nothing; the Como tour is
    a day later and untouched. Both must read as safe."""
    assert impact.by_id("dinner").severity is Severity.SAFE
    assert impact.by_id("dinner").exposure == 0.0
    assert impact.by_id("como").severity is Severity.SAFE


def test_countdown_anchors_to_a_real_deadline(impact):
    """The UI currently counts down from a hard-coded integer. This is what
    replaces it."""
    when, node = impact.next_cutoff
    assert node.id == "transfer"
    assert when - NOW == timedelta(hours=3, minutes=22)


def test_a_leg_they_can_still_take_carries_them_to_the_next_one():
    """The walk has to walk. Judging every booking against the disruption point
    alone means the only way onward is `transit`, which knows ground routes and
    not flights -- so a trip that flies on past the connection lost everything
    after it.

    Zurich to Milan to Rome, all one day so nothing is rescued by "later days
    look after themselves". The Zurich leg lands 90 minutes late and the 14:00
    onward hop is still comfortably catchable; it is the thing that puts the
    traveller in Rome. Before this, the same table said the flight to Rome was
    safe and the meeting in Rome was missed.
    """
    day = datetime(2026, 10, 12, tzinfo=CEST)

    def at(hour, minute=0):
        return day.replace(hour=hour, minute=minute)

    onward = Booking(id="l2", kind=Kind.FLIGHT, provider="AZ",
                     title="AZ 2050 MXP to FCO", start=at(14), end=at(15, 20),
                     origin="MXP", destination="FCO", price=140.0)
    room = Booking(id="h2", kind=Kind.LODGING, provider="hotel",
                   title="Rome hotel", start=at(14), end=at(23), origin="FCO",
                   price=430.0, hard_deadline=at(22),
                   mitigation="call the property to hold the room")
    client = Booking(id="m2", kind=Kind.ACTIVITY, provider="calendar",
                     title="Rome client meeting", start=at(18), end=at(19),
                     origin="FCO", price=0.0, commitment=True, who="the client")

    trip = Trip([Booking(id="l1", kind=Kind.FLIGHT, provider="LX",
                         title="LX 1608 ZRH to MXP", start=at(9), end=at(10, 10),
                         origin="ZRH", destination="MXP", price=170.0),
                 onward, room, client])
    late = Disruption("l1", at(11, 40), "late inbound aircraft", 0.9)
    impact = propagate(trip, late, at(8))

    severities = {n.booking.id: n.severity for n in impact.nodes}
    assert severities["l2"] is Severity.SAFE, "the premise: they still make it"
    assert severities["h2"] is Severity.SAFE
    assert severities["m2"] is Severity.SAFE
    assert impact.do_nothing_cost == 0.0 and impact.at_risk_value == 0.0


def test_a_leg_they_cannot_take_carries_them_nowhere():
    """The other half, and the reason this only ever forgives. Same trip, with
    the onward hop missed -- Rome has to go back to being out of reach, or
    chaining would have quietly rescued the whole itinerary."""
    day = datetime(2026, 10, 12, tzinfo=CEST)

    def at(hour, minute=0):
        return day.replace(hour=hour, minute=minute)

    trip = Trip([
        Booking(id="l1", kind=Kind.FLIGHT, provider="LX",
                title="LX 1608 ZRH to MXP", start=at(9), end=at(10, 10),
                origin="ZRH", destination="MXP", price=170.0),
        Booking(id="l2", kind=Kind.FLIGHT, provider="AZ",
                title="AZ 2050 MXP to FCO", start=at(14), end=at(15, 20),
                origin="MXP", destination="FCO", price=140.0),
        Booking(id="m2", kind=Kind.ACTIVITY, provider="calendar",
                title="Rome client meeting", start=at(18), end=at(19),
                origin="FCO", price=0.0, commitment=True, who="the client"),
    ])
    # Cancelled, so they never leave Zurich and the onward hop goes with it.
    stuck = Disruption("l1", at(8, 30), "cancelled", 1.0, cancelled=True)
    impact = propagate(trip, stuck, at(8))

    severities = {n.booking.id: n.severity for n in impact.nodes}
    assert severities["l2"] is Severity.BROKEN
    assert severities["m2"] is Severity.BROKEN


def test_nothing_is_recoverable_once_every_window_has_passed():
    """Same disruption, discovered after the fact. The map must go quiet.

    18:00 rather than 09:00, which is where this test started. At 09:00 the
    museum date-change window is still open and worth 74, so the original
    expectation of zero was simply wrong -- the engine was right and the test
    was not. The last window to close is the 16:00 entry slot.
    """
    late = propagate(TRIP, DISRUPTION, datetime(2026, 10, 12, 18, 0, tzinfo=CEST))
    assert late.act_now_value == 0.0
    assert late.do_nothing_cost == 423.0
