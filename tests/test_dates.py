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


def _two_recordings(tmp_path, monkeypatch):
    folder = tmp_path / "status"
    folder.mkdir()
    (folder / "flight-XX1_on-2026-10-11.json").write_text('{"which": "october"}')
    (folder / "flight-XX1_on-2026-09-18.json").write_text('{"which": "september"}')
    monkeypatch.setattr(ports_base, "FIXTURES", tmp_path)


def test_a_caller_can_name_the_recording_it_means(tmp_path, monkeypatch):
    """Two recordings of one route are not interchangeable -- they are two
    different days' inventory. The scripted demo's figures are asserted by tests
    and shown to judges, so it asks for its own capture and keeps them whatever
    date the trip is anchored to."""
    _two_recordings(tmp_path, monkeypatch)
    got = ports_base.call("status", {"flight": "XX1", "on": "2026-09-01"},
                          lambda: None, prefer=date(2026, 10, 11))
    assert got == {"which": "october"}


def test_without_a_hint_the_nearest_recording_wins(tmp_path, monkeypatch):
    """The shift is the distortion, so the smallest one is the least wrong. A
    booking search three weeks out has to land on the capture made for three
    weeks out, not on a scenario fixture from six weeks later -- and "newest
    file" would have picked the latter, because the date in a fixture name is
    the day the search was FOR, not the day it was captured."""
    _two_recordings(tmp_path, monkeypatch)
    assert ports_base.call("status", {"flight": "XX1", "on": "2026-09-01"},
                           lambda: None) == {"which": "september"}
    assert ports_base.call("status", {"flight": "XX1", "on": "2026-10-20"},
                           lambda: None) == {"which": "october"}


def test_choosing_between_recordings_is_announced(tmp_path, monkeypatch):
    """The original worry was a silent wrong answer. Refusing outright was the
    wrong remedy -- recording again adds a THIRD candidate and the next run
    fails identically, which is exactly how the scripted demo broke the morning
    after a second ZRH -> MXP search was captured. This is the safeguard that
    replaced it."""
    _two_recordings(tmp_path, monkeypatch)
    ports_base.shifted.clear()
    ports_base.call("status", {"flight": "XX1", "on": "2026-09-01"}, lambda: None)
    assert any("chosen from 2 recordings" in note for note in ports_base.shifted)


def test_no_recording_at_all_still_refuses(tmp_path, monkeypatch):
    """The failure that re-recording actually fixes keeps saying so."""
    (tmp_path / "status").mkdir()
    monkeypatch.setattr(ports_base, "FIXTURES", tmp_path)
    with pytest.raises(ports_base.PortError, match="record.py"):
        ports_base.call("status", {"flight": "XX1", "on": "2026-09-01"}, lambda: None)


def test_the_scripted_demo_is_unambiguous_on_any_day():
    """The regression. `--base today` searched ZRH -> MXP for today's date, found
    two recordings of that route and refused -- so the default page broke the
    day after a second one was captured."""
    import duffel
    from demo_trip import as_written

    for basis in (None, date(2026, 8, 29), date(2026, 12, 25)):
        day = anchor(basis, 12, 9, 0)
        found = duffel.offers("ZRH", "MXP", day, prefer=as_written(12))
        assert found, f"no offers for basis {basis}"
        assert all(o.depart.date() == day.date() for o in found)


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

    assert cost(None) == cost(DEFAULT_BASE + timedelta(days=SHIFT)) == 423.0
