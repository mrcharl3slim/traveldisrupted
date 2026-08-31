"""Deadlines, and when somebody has to be told about them.

Everything before this file answers a question you have to think to ask. This
one is the product: the deadline that decides whether tonight is fixable passes
while the traveller is asleep on an aircraft, and nobody wakes them for it. An
engine you have to open is a report. An engine that speaks first is a service.

WHAT AN ALERT IS ALLOWED TO BE. One deadline, with money or a bed behind it.
That constraint is the whole design. A disrupted itinerary has a dozen
timestamps in it and a product that fires on all of them is one nobody keeps
installed past the second trip -- so an alert exists only where the clock
running out actually takes something away:

  * a fare rule whose window still returns more than it costs to use, and
  * the last moment a replacement could still land in time.

A deadline with nothing behind it is not an alert. It is a fact, and it stays
in the impact graph where anyone who wants it can look.

WHY THE SCHEDULE IS COMPUTED RATHER THAN QUEUED. Nothing here writes a job,
sets a timer or remembers anything. `schedule` is a pure function of the trip
and the disruption, so the same call answers "what is coming" for a page and
"what is due" for a ticker, and the two cannot disagree. Firing once is the
caller's watermark, not a flag hidden in here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from domain import Disruption, Trip
from graph import Impact, Severity, propagate
from plan import Gap, recovery_gap

#: How far ahead of a deadline to speak up. An hour is long enough to phone a
#: property or reach a checkout; a quarter of an hour is the last honest moment
#: to say "decide now". Anything more often is noise wearing a uniform.
LEADS: tuple[timedelta, ...] = (timedelta(minutes=60), timedelta(minutes=15))

#: Below this, saying something costs the traveller more attention than the
#: deadline costs money. Stated as a number so the judgement is arguable rather
#: than buried in a condition.
FLOOR = 1.0


@dataclass(frozen=True)
class Alert:
    at: datetime          # when to say it
    closes: datetime      # the deadline it is about
    booking_id: str
    title: str
    kind: str             # "lead" | "final"
    worth: float          # what stops being possible when `closes` passes
    message: str

    @property
    def key(self) -> str:
        """Stable across recomputation, so a fired alert stays fired.

        Derived from the deadline rather than from position in a list: the
        schedule is rebuilt on every tick, and an index would renumber itself
        the moment a window closed and the alert behind it would fire twice.

        TRIP-LOCAL, and it has to be qualified before it is used anywhere else.
        Booking ids come from the title when a trip is read back, so two
        itineraries containing the same hotel on the same night produce the
        same key -- correctly, because they are two separate things at stake,
        and confusingly for anything that dedupes across trips. The watermark
        is stored per trip, so firing is right; a log or a cache keyed on this
        alone would conflate them.
        """
        return f"{self.booking_id}|{self.closes.isoformat()}|{self.kind}|{self.lead}"

    @property
    def lead(self) -> int:
        return int((self.closes - self.at).total_seconds() // 60)


def _fire_times(closes: datetime) -> list[tuple[datetime, str]]:
    return [(closes - lead, "lead") for lead in LEADS] + [(closes, "final")]


def _money(worth: float) -> str:
    return f"S${worth:,.0f}"


def _for_window(node, closes: datetime, worth: float, label: str) -> list[Alert]:
    out = []
    for at, kind in _fire_times(closes):
        lead = int((closes - at).total_seconds() // 60)
        message = (
            f"{_money(worth)} on {node.booking.title} stops being recoverable "
            f"in {lead} minutes, at {closes:%H:%M}"
            if kind == "lead" else
            f"{_money(worth)} on {node.booking.title} is gone as of "
            f"{closes:%H:%M}")
        out.append(Alert(at=at, closes=closes, booking_id=node.id,
                         title=node.booking.title, kind=kind, worth=worth,
                         message=message + (f" — {label}" if label else "")))
    return out


def _for_gap(gap: Gap, node) -> list[Alert]:
    out = []
    for at, kind in _fire_times(gap.by):
        lead = int((gap.by - at).total_seconds() // 60)
        message = (
            f"{lead} minutes left to be on something that reaches "
            f"{gap.destination} by {gap.by:%H:%M} — after that "
            f"{node.booking.title} is unreachable whatever you spend"
            if kind == "lead" else
            f"Nothing can reach {gap.destination} in time now. "
            f"{node.booking.title} is beyond rescue as of {gap.by:%H:%M}")
        out.append(Alert(at=at, closes=gap.by, booking_id=gap.for_booking,
                         title=node.booking.title, kind=kind,
                         worth=node.exposure, message=message))
    return out


def schedule(trip: Trip, disruption: Disruption, now: datetime,
             impact: Impact | None = None) -> list[Alert]:
    """Every moment somebody needs to hear something, in order.

    Includes fire times already in the past. That is deliberate: a traveller
    opening the page at 03:00 has to see the 01:00 deadline they slept through
    as well as the 06:00 one they can still make, and a schedule that quietly
    drops what it failed to deliver is the one bug in a notifier nobody
    catches. `due` decides what to say now; this decides what is true.
    """
    impact = impact or propagate(trip, disruption, now)
    found: dict[str, Alert] = {}

    def keep(alert: Alert) -> None:
        """One moment, one sentence -- the most expensive thing it takes away.

        Two different deadlines land on the same booking at the same minute
        more often than seems likely: the museum's S$27 date change and the
        museum itself both expire at 16:00. Sending both is two notifications
        about one clock. Keeping whichever is worth more is not a tiebreak, it
        is the right sentence: "S$138 of entry is gone" is the fact, and "the
        S$111 change window closed" is a detail of how.
        """
        current = found.get(alert.key)
        if current is None or alert.worth > current.worth:
            found[alert.key] = alert

    for node in impact.nodes:
        if node.severity not in (Severity.BROKEN, Severity.AT_RISK):
            continue
        for window in node.booking.policy.windows:
            worth = window.net
            if worth < FLOOR:
                continue          # a deadline with nothing behind it
            for alert in _for_window(node, window.closes, worth, window.label):
                keep(alert)

    gap = recovery_gap(trip, disruption, now)
    if gap is not None:
        node = next((n for n in impact.nodes if n.id == gap.for_booking), None)
        if node is not None and node.exposure >= FLOOR:
            for alert in _for_gap(gap, node):
                keep(alert)

    return sorted(found.values(), key=lambda a: (a.at, -a.worth, a.booking_id))


def due(alerts: list[Alert], since: datetime | None, now: datetime) -> list[Alert]:
    """What to say on this tick, and nothing twice.

    ``since`` is the watermark the caller last acted on. Passing None means a
    first run, and a first run on an already-late trip has to say what has
    already been missed rather than pretending the day started now.
    """
    if since is None:
        return [a for a in alerts if a.at <= now]
    return [a for a in alerts if since < a.at <= now]


def next_up(alerts: list[Alert], now: datetime) -> Alert | None:
    return next((a for a in alerts if a.at > now), None)
