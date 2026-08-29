"""Doing the thing, and being exact about which part was done.

Every recovery plan in this system sorts its actions into three lanes: what
Downstream can perform, what the traveller authorises with one tap, and what
still needs a phone call because no consumer API exists. Until this file, all
three lanes were labels. A plan that recommends emailing a hotel and then does
not email the hotel is a report with buttons on it.

WHAT ACTING MEANS HERE, PRECISELY:

  AUTO  performed. The message is sent through whatever channel is configured,
        and the result records which channels actually took it -- possibly
        none, which is reported rather than hidden.
  TAP   handed over. A deep link and everything needed to complete it, marked
        pending. Nothing is bought. Duffel's test mode cannot take money and we
        would not use it if it could: a demo that spends is a demo nobody runs
        twice.
  CALL  handed over with a script. There is no API. Saying so is the feature.

AND THE ITINERARY IS REWRITTEN. This is the part that makes it adaptation
rather than notification. The stored trip becomes the trip the traveller now
has: the cancelled leg leaves, the replacement arrives with the fare that was
quoted, moved slots move. Come back tomorrow and the itinerary reflects the
decision, because a plan you approved and an itinerary that still shows the old
flight is a system that has quietly disagreed with itself.

WHAT IS DELIBERATELY NOT CLAIMED. The replacement leg enters the itinerary
marked `pending` until the traveller completes the purchase in the provider's
own checkout. It is a real booking in the plan and an intention in the record,
and the two are not allowed to look alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

import notify
from domain import Booking, Kind, Trip
from plan import Action, Lane, Plan

#: Where a tap actually goes. Airlines do not publish a "book this offer id"
#: consumer endpoint, so this is a search deep link with the query pre-filled --
#: which is what every "one-tap booking" in this market actually is, said
#: plainly rather than implied.
BOOK_URL = ("https://www.google.com/travel/flights?q="
            "Flights%20from%20{origin}%20to%20{destination}%20on%20{day}")


@dataclass
class Done:
    """What happened, per action, in the traveller's words rather than ours."""

    verb: str
    label: str
    lane: str
    state: str                    # "sent" | "pending" | "nothing to send"
    channels: list[str] = field(default_factory=list)
    link: str = ""
    note: str = ""


@dataclass
class Outcome:
    plan_id: str
    performed: list[Done]
    trip: Trip | None
    changed: list[str]
    reply: str

    @property
    def sent(self) -> int:
        return sum(1 for d in self.performed if d.state == "sent")

    @property
    def pending(self) -> int:
        return sum(1 for d in self.performed if d.state == "pending")


def _link(plan: Plan, action: Action) -> str:
    if action.verb != "buy" or not plan.arrives_at or not plan.arrives_where:
        return ""
    return BOOK_URL.format(origin=getattr(plan, "departs_from", "") or "",
                           destination=plan.arrives_where,
                           day=f"{plan.arrives_at:%Y-%m-%d}")


def perform(plan: Plan, trip: Trip, trip_id: str = "",
            log=print) -> list[Done]:
    """Run every action in the plan, each according to its lane."""
    out: list[Done] = []
    for action in plan.actions:
        if action.lane is Lane.AUTO and action.verb == "notify":
            channels = notify.deliver(_as_alert(action, trip), trip_id, log=log)
            out.append(Done(action.verb, action.label, action.lane.value,
                            "sent", channels, note=action.note))
        elif action.lane is Lane.AUTO:
            # monitor: real, continuous, and nothing leaves the building for it.
            out.append(Done(action.verb, action.label, action.lane.value,
                            "nothing to send", note=action.note))
        else:
            out.append(Done(action.verb, action.label, action.lane.value,
                            "pending", link=_link(plan, action),
                            note=action.note))
    return out


class _AlertShim:
    """`notify.deliver` speaks Alert. An action is close enough to say so.

    A shim rather than a second renderer: one place decides how a message
    reads, and a second one would drift from it by the second demo.
    """

    def __init__(self, action: Action, closes: datetime, title: str):
        self.kind = "final" if action.verb == "cancel" else "lead"
        self.closes = closes
        self.message = action.label + (f" — {action.note}" if action.note else "")
        self.lead = 0
        self.title = title
        self.booking_id = action.booking_id or ""
        self.worth = action.cash_in


def _as_alert(action: Action, trip: Trip):
    when = action.deadline or datetime.now(trip.in_order()[0].start.tzinfo)
    title = ""
    if action.booking_id:
        try:
            title = trip.by_id(action.booking_id).title
        except KeyError:
            title = ""
    return _AlertShim(action, when, title)


def apply(plan: Plan, trip: Trip, disruption, offer=None) -> tuple[Trip, list[str]]:
    """The itinerary the traveller has after taking this plan.

    Three edits, all derived from the plan rather than from a form: the
    disrupted booking goes, anything the plan gave up on goes, and the
    replacement leg arrives marked pending. Nothing else is touched -- a
    recovery that quietly reorganises the rest of the week is a recovery nobody
    trusts twice.
    """
    from builder import flight

    dropped = {disruption.booking_id} | set(plan.wasted_ids)
    kept = [b for b in trip.in_order() if b.id not in dropped]
    changed = [f"removed {trip.by_id(b).title}"
               for b in sorted(dropped) if _has(trip, b)]

    if offer is not None:
        added = replace(flight(offer), pending=True)
        kept.append(added)
        changed.append(f"added {added.title} (pending purchase)")

    for booking_id in sorted(plan.delivered):
        if _has(trip, booking_id):
            changed.append(f"kept {trip.by_id(booking_id).title}")

    return Trip(sorted(kept, key=lambda b: b.start)), changed


def _has(trip: Trip, booking_id: str) -> bool:
    try:
        trip.by_id(booking_id)
        return True
    except KeyError:
        return False


def summarise(done: list[Done], changed: list[str]) -> str:
    sent = [d for d in done if d.state == "sent"]
    pending = [d for d in done if d.state == "pending"]
    bits = []
    if sent:
        where = sorted({c for d in sent for c in d.channels})
        bits.append(f"{len(sent)} sent ({', '.join(where) or 'no channel took it'})")
    if pending:
        bits.append(f"{len(pending)} waiting on you")
    removed = len([c for c in changed if c.startswith("removed")])
    added = len([c for c in changed if c.startswith("added")])
    if removed or added:
        bits.append(f"itinerary updated: {removed} removed, {added} added")
    return " · ".join(bits) or "nothing to do on this plan"
