"""The tests that make the demo defensible.

A judge's fair question is "did you compute EUR 282 or type it?". These are the
answer. Every expected value below is one a human can verify from the fare
rules by hand, and none of them appears as a literal anywhere in the engine.
"""

from datetime import datetime, timedelta, timezone

import pytest

from demo_trip import TRIP, CEST, dt
from domain import Disruption
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
    """142 + 48 + 92. If this drifts, the pitch is wrong, not the test."""
    assert impact.do_nothing_cost == 282.0
    assert {n.id for n in impact.broken} == {"lx1608", "transfer", "lastsupper"}


def test_acting_is_worth_less_than_doing_nothing_costs(impact):
    """38 taxes + 48 transfer + (92 - 18) date change = 160.

    Worth stating explicitly: recovery never returns the full 282. Selling
    otherwise would be a lie the fare rules do not support.
    """
    assert impact.act_now_value == 160.0


def test_missed_connection_is_reachability_not_a_flag(impact):
    """Nothing marks LX 1608 critical. 10:25 + 30 min boarding > 08:20 does."""
    assert impact.by_id("lx1608").severity is Severity.BROKEN
    assert impact.by_id("lx1608").recoverable == 38.0


def test_transfer_window_closes_while_the_traveller_is_airborne(impact):
    """The quiet EUR 48. Its deadline is four hours before a pickup nobody
    will make, and it passes at 06:00 -- mid-flight, hours before landing."""
    node = impact.by_id("transfer")
    assert node.severity is Severity.BROKEN
    assert node.cutoff == dt(12, 6, 0)
    assert node.cutoff < DISRUPTION.new_end


def test_hotel_is_at_risk_not_lost(impact):
    """A message defuses it, so it must never be counted in the 282."""
    node = impact.by_id("hotel")
    assert node.severity is Severity.AT_RISK
    assert node.exposure == 624.0
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


def test_nothing_is_recoverable_once_every_window_has_passed():
    """Same disruption, discovered after the fact. The map must go quiet.

    18:00 rather than 09:00, which is where this test started. At 09:00 the
    museum date-change window is still open and worth 74, so the original
    expectation of zero was simply wrong -- the engine was right and the test
    was not. The last window to close is the 16:00 entry slot.
    """
    late = propagate(TRIP, DISRUPTION, datetime(2026, 10, 12, 18, 0, tzinfo=CEST))
    assert late.act_now_value == 0.0
    assert late.do_nothing_cost == 282.0
