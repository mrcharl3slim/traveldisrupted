"""Anchoring the trip to run time.

Live flight status covers roughly a week either side of today, so a scenario
pinned to 12 October cannot be demonstrated live in September. Moving it is
therefore not a convenience -- it is what makes "live detection" a checkable
claim. These tests exist because moving a calendar is exactly the kind of change
that appears to work and quietly alters an answer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import aerodatabox
import base as ports_base
from demo_trip import DEFAULT_BASE, anchor, build_trip, shift_trip
from graph import propagate

SHIFT = -46


def _shifted():
    return build_trip(DEFAULT_BASE + timedelta(days=SHIFT))


# -- the trip -------------------------------------------------------------

def test_every_interval_survives_the_move():
    a, b = build_trip().in_order(), _shifted().in_order()
    assert len(a) == len(b)
    for i in range(1, len(a)):
        assert (a[i].start - a[i - 1].start) == (b[i].start - b[i - 1].start)


def test_the_structural_flaw_survives_the_move():
    """Two ticket groups on the Singapore and Zurich legs is the whole
    scenario. If a refactor ever merges them the demo has no premise."""
    groups = {b.id: b.ticket_group for b in _shifted().in_order() if b.ticket_group}
    assert groups["sq346"] != groups["lx1608"]


def test_hard_deadline_moves_with_the_booking():
    """Regression. Shifting only start and end left the hotel's arrival
    guarantee six weeks after the flight meant to reach it, and the hotel
    reported itself SAFE -- a wrong answer rather than a crash."""
    hotel = [b for b in _shifted().in_order() if b.hard_deadline][0]
    original = [b for b in build_trip().in_order() if b.id == hotel.id][0]
    assert (hotel.hard_deadline - original.hard_deadline).days == SHIFT
    assert hotel.hard_deadline.date() == hotel.start.date()


def test_fare_windows_move_with_the_booking():
    def windows(trip):
        return [w.closes for b in trip.in_order()
                for w in (b.policy.windows if b.policy and b.policy.windows else [])]
    for before, after in zip(windows(build_trip()), windows(_shifted())):
        assert (after - before).days == SHIFT


def test_shift_of_zero_returns_the_trip_untouched():
    assert build_trip(DEFAULT_BASE) is build_trip()
    assert shift_trip(build_trip(), 0) is build_trip()


# -- the replay layer -----------------------------------------------------

def test_a_recording_is_replayed_under_a_different_date():
    ports_base.shifted.clear()
    payload = ports_base.call(
        "status", {"flight": "SQ346", "on": "2026-08-26"}, lambda: None)
    assert payload
    assert ports_base.shifted, "a date-shifted replay must announce itself"


def test_replayed_dates_are_moved_but_times_are_not():
    exact = ports_base.call("status", {"flight": "SQ346", "on": "2026-10-11"}, lambda: None)
    moved = ports_base.call("status", {"flight": "SQ346", "on": "2026-08-26"}, lambda: None)
    a = aerodatabox.disruption("SQ346", datetime(2026, 10, 11, 9, 0), "sq346")
    b = aerodatabox.disruption("SQ346", datetime(2026, 8, 26, 9, 0), "sq346")
    assert (b.new_end.date() - a.new_end.date()).days == SHIFT
    assert b.new_end.timetz() == a.new_end.timetz()
    assert exact and moved


def test_an_ambiguous_directory_refuses_rather_than_guessing(tmp_path, monkeypatch):
    """Two recordings differing only by date: there is no way to know which was
    meant, and picking one would answer with the wrong day's flight."""
    folder = tmp_path / "status"
    folder.mkdir()
    (folder / "flight-XX1_on-2026-10-11.json").write_text("{}")
    (folder / "flight-XX1_on-2026-10-12.json").write_text("{}")
    monkeypatch.setattr(ports_base, "FIXTURES", tmp_path)
    with pytest.raises(ports_base.PortError, match="ambiguous"):
        ports_base.call("status", {"flight": "XX1", "on": "2026-09-01"}, lambda: None)


def test_date_regex_ignores_longer_digit_runs():
    assert ports_base._ISO.sub("D", "from-ZRH_on-2026-10-12_to-MXP") == "from-ZRH_on-D_to-MXP"
    assert ports_base._ISO.sub("D", "id-12026-10-123") == "id-12026-10-123"


# -- the conclusion -------------------------------------------------------

def test_moving_the_calendar_changes_no_conclusion():
    """The point of all of the above, stated once."""
    def summarise(basis):
        trip = build_trip(basis)
        disruption = aerodatabox.disruption("SQ346", anchor(basis, 11, 9, 0), "sq346")
        impact = propagate(trip, disruption, anchor(basis, 12, 2, 38))
        return [(n.booking.id, n.severity, round(n.exposure, 2), round(n.recoverable, 2))
                for n in impact.nodes]

    assert summarise(None) == summarise(DEFAULT_BASE + timedelta(days=SHIFT))


def test_do_nothing_cost_is_unchanged_by_the_shift():
    def cost(basis):
        trip = build_trip(basis)
        disruption = aerodatabox.disruption("SQ346", anchor(basis, 11, 9, 0), "sq346")
        return propagate(trip, disruption, anchor(basis, 12, 2, 38)).do_nothing_cost

    assert cost(None) == cost(DEFAULT_BASE + timedelta(days=SHIFT)) == 282.0
