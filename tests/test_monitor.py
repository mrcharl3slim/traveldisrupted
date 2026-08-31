"""The watch: what gets said, when, once, and to whom.

A notifier is easy to write and hard to trust. Every test here is about the
ways one stops being trustworthy without failing: it fires twice, it fires on
deadlines with nothing behind them, it goes quiet after a restart, or it
reports a delivery that never left the building.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import monitor
import notify
from demo_trip import TRIP, dt
from domain import Disruption
from graph import propagate

NOW = dt(12, 2, 38)
DELAYED = Disruption("sq346", dt(12, 10, 25), "late inbound aircraft", 0.91)


@pytest.fixture
def alerts():
    return monitor.schedule(TRIP, DELAYED, NOW)


def test_every_alert_has_money_or_a_bed_behind_it(alerts):
    """The constraint the whole design rests on. A disrupted itinerary has a
    dozen timestamps in it, and a product that fires on all of them is one
    nobody keeps installed past the second trip."""
    assert alerts
    assert all(a.worth >= monitor.FLOOR for a in alerts)


def test_nothing_is_announced_that_the_impact_graph_calls_safe(alerts):
    """One source of truth for what is at stake. If the watch could alert on a
    booking the graph says is fine, the two have started disagreeing and the
    page will show both."""
    impact = propagate(TRIP, DELAYED, NOW)
    at_stake = {n.id for n in impact.nodes
                if n.severity.value in ("critical", "risk")}
    assert {a.booking_id for a in alerts} <= at_stake


def test_each_deadline_gets_warned_about_before_it_arrives(alerts):
    """A notification that arrives at the deadline is a receipt."""
    for closes in {a.closes for a in alerts}:
        leads = sorted(a.lead for a in alerts if a.closes == closes)
        assert leads[0] == 0                       # the deadline itself
        assert leads[-1] == 60                     # and an hour of warning


def test_one_moment_gets_one_sentence(alerts):
    """The museum's EUR 18 date change and the museum itself both expire at
    16:00. Two notifications about one clock is how a product teaches somebody
    to ignore it."""
    stamps = [(a.booking_id, a.at) for a in alerts]
    assert len(stamps) == len(set(stamps))

    final = next(a for a in alerts
                 if a.booking_id == "lastsupper" and a.kind == "final")
    # The bigger loss wins: S$138 of entry gone, not a S$111 window closing.
    assert final.worth == 138.0


def test_the_schedule_keeps_what_it_failed_to_deliver():
    """A traveller opening the page at 03:00 has to see the 01:00 deadline they
    slept through. A schedule that drops what it missed is the one bug in a
    notifier nobody catches."""
    late = monitor.schedule(TRIP, DELAYED, dt(12, 9, 0))
    assert any(a.at < dt(12, 9, 0) for a in late)


def test_a_first_run_says_what_was_already_missed(alerts):
    """No watermark means nobody has been told anything, which is not the same
    as nothing having happened."""
    ready = monitor.due(alerts, None, dt(12, 8, 30))
    assert ready and all(a.at <= dt(12, 8, 30) for a in ready)


def test_nothing_is_said_twice(alerts):
    """The schedule is rebuilt from scratch on every pass, so 'once' has to
    come from the watermark rather than from a flag in the loop."""
    first = monitor.due(alerts, None, dt(12, 6, 0))
    mark = max(a.at for a in first)
    second = monitor.due(alerts, mark, dt(12, 6, 0))
    assert first and second == []

    later = monitor.due(alerts, mark, dt(12, 8, 30))
    assert later and not (set(a.key for a in later) & set(a.key for a in first))


def test_an_alert_key_survives_recomputation(alerts):
    """Keys are derived from the deadline, not from position in a list --
    otherwise a closing window renumbers everything behind it and the alerts
    after it fire a second time."""
    again = monitor.schedule(TRIP, DELAYED, dt(12, 5, 30))
    shared = {a.key for a in alerts} & {a.key for a in again}
    assert len(shared) >= 3


def test_a_quiet_trip_schedules_nothing():
    """Cancelling the last leg strands nobody and closes no window. Silence is
    the answer, not a failure."""
    quiet = Disruption("sq345", dt(17, 13, 40), "cancelled", 1.0, cancelled=True)
    assert monitor.schedule(TRIP, quiet, dt(17, 12, 0)) == []


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------


def test_an_unconfigured_channel_is_reported_as_absent(monkeypatch):
    monkeypatch.delenv("DOWNSTREAM_WEBHOOK", raising=False)
    assert notify.channels() == {"log": True, "webhook": False}


def test_delivery_reports_only_what_actually_took_it(alerts, monkeypatch):
    """The lane rule turned on the notifier itself. A channel that is not
    configured must not swallow the message and report success."""
    monkeypatch.delenv("DOWNSTREAM_WEBHOOK", raising=False)
    lines = []
    sent = notify.deliver(alerts[0], "trip123", log=lines.append)
    assert sent == ["log"]
    assert lines and "trip123" in lines[0]


def test_a_dead_webhook_does_not_take_the_watch_down(alerts, monkeypatch):
    monkeypatch.setenv("DOWNSTREAM_WEBHOOK", "http://127.0.0.1:9/nowhere")
    assert notify.channels()["webhook"] is True
    assert notify.deliver(alerts[0], log=lambda _: None) == ["log"]


def test_a_missed_deadline_reads_as_missed(alerts):
    final = next(a for a in alerts if a.kind == "final")
    assert notify.render(final).startswith("[MISSED]")
    lead = next(a for a in alerts if a.kind == "lead")
    assert notify.render(lead).startswith("[T-")
