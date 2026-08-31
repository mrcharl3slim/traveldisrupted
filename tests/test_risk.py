"""Where the trip is thin, said before anything breaks.

Every flag here is derived -- slack arithmetic on booked times, or the
engine's own propagate run over hypothetical delays -- and the tests hold the
module to that: no probabilities, no money in the prose, and no flag for a
"connection" two days wide.
"""

from __future__ import annotations

from datetime import timedelta

import risk
from demo_trip import build_trip

TRIP = build_trip(None)


def test_the_scenario_premise_is_flagged_before_it_happens():
    """The scripted demo's whole story is two tickets and a missed connection.
    That must be visible at BOOKING time: separate purchases, minutes to
    spare, nobody owes you the second leg."""
    found = risk.assess(TRIP)
    unprotected = [r for r in found if r.kind == "unprotected"]
    assert unprotected, "the premise went unflagged"
    first = unprotected[0]
    assert first.margin_minutes == 75
    assert "separate purchases" in first.sentence
    assert "nobody owes you" in first.sentence


def test_a_train_two_days_later_is_not_a_connection():
    """True-but-useless flags bury the one that matters. The Florence trains
    are separate tickets AND days apart; only dependent legs count."""
    found = risk.assess(TRIP)
    for r in found:
        if r.kind == "unprotected":
            assert r.margin_minutes < int(risk.DEPENDENT.total_seconds() // 60)


def test_the_breakpoints_come_from_the_engine_not_a_guess():
    """The smallest delay that starts costing money, one propagate per rung.
    The onward hop breaks at the first rung -- its transfer window is the
    quiet S$72 -- and the long-haul survives an hour."""
    found = {r.booking_id: r for r in risk.assess(TRIP) if r.kind == "breakpoint"}
    assert found["lx1608"].breaks_at_minutes == 30
    assert found["lx1608"].worth and found["lx1608"].worth > 0
    assert found["sq346"].breaks_at_minutes == 90


def test_money_never_leaks_into_the_prose():
    """The sentence is figure-free and the number lives in `worth`, so role
    redaction keeps working by key instead of by parsing prose."""
    for r in risk.assess(TRIP):
        assert "S$" not in r.sentence and "$" not in r.sentence


def test_the_thinnest_place_reads_first():
    found = risk.assess(TRIP)
    keys = [r.margin_minutes if r.margin_minutes is not None
            else r.breaks_at_minutes for r in found]
    assert keys == sorted(keys)


def test_a_trip_with_slack_everywhere_has_nothing_to_say():
    from domain import Booking, Kind, Trip
    from datetime import datetime, timezone
    day = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    lone = Trip([Booking(id="l1", kind=Kind.FLIGHT, provider="X", title="A to B",
                         start=day, end=day + timedelta(hours=2),
                         origin="AAA", destination="BBB", price=100.0)])
    assert risk.assess(lone) == []
