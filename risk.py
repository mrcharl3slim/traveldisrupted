"""Where the trip is thin, said before anything breaks.

Everything else in this engine speaks after a disruption: propagate answers
"what just broke", the watch counts down deadlines that already exist. This
module reads the SAME itinerary with the same arithmetic and answers the
question a traveller has at booking time -- where would this trip snap?

THREE KINDS OF FLAG, ALL DERIVED, NONE PREDICTED. There is no weather feed,
no delay statistics and no probability model here, and pretending otherwise
would be inventing the one thing this product exists not to invent. What can
be said honestly:

  connection   the slack between landing and the next leg's last check-in,
               where it is thinner than TIGHT. Arithmetic on booked times.
  unprotected  consecutive legs on separate tickets: two contracts, so no
               airline owes the connection -- the scripted scenario's whole
               premise, surfaced BEFORE it happens instead of after.
  breakpoint   the smallest delay on each leg that starts costing money,
               found by running the engine's own propagate over a ladder of
               hypothetical delays. Not a prediction that it WILL happen; a
               statement of what happens IF, from the same arithmetic that
               will price it on the day.

THE THRESHOLD IS A JUDGMENT CONSTANT AND SAYS SO, exactly like
builder.MIN_CONNECTION: below 90 minutes of slack a connection is flagged.
Anything under MIN_CONNECTION was refused at selection; this speaks about the
band above it, where the trip is sellable and still thin.

Money goes in the `worth` field and never in the sentence, so the role
redaction that blanks monetary keys keeps working without parsing prose.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta

import flow
from domain import Booking, Kind, Trip, transit
from graph import propagate

#: Slack thinner than this is worth saying out loud. A judgment constant,
#: named and visible, like MIN_CONNECTION -- not a probability.
TIGHT = timedelta(minutes=90)

#: Beyond this much slack, two legs are not a connection in any sense worth
#: warning about. An overnight decouples them: a train two days after landing
#: is a separate journey, and flagging it would bury the one flag that matters
#: under a page of true-but-useless ones. Anything inside the same travel day
#: still counts -- a separate-ticket leg six hours after landing is exactly
#: the kind of dependency that quietly snaps.
DEPENDENT = timedelta(hours=12)

#: The delay ladder the stress test climbs. Stops at six hours: a trip that
#: absorbs six hours is not thin, and past that the answer is the abandon
#: ledger, not a flag.
LADDER = (30, 60, 90, 120, 180, 240, 360)

TRANSPORT = (Kind.FLIGHT, Kind.RAIL)


@dataclass(frozen=True)
class Risk:
    kind: str              # "connection" | "unprotected" | "breakpoint"
    booking_id: str
    sentence: str          # figure-free; the money lives in `worth`
    margin_minutes: int | None = None
    breaks_at_minutes: int | None = None
    worth: float | None = None      # S$ at stake, redactable by key


def _margin(prev: Booking, nxt: Booking) -> timedelta | None:
    """Minutes to spare between arriving off ``prev`` and the last moment
    ``nxt`` will wait, ground time included. None when the ground route is
    unknown -- an absence, not a zero."""
    if prev.end is None:
        return None
    ground = transit(prev.destination or prev.where, nxt.where)
    if ground is None:
        return None
    return nxt.must_arrive_by - (prev.end + ground)


def _connections(trip: Trip) -> list[Risk]:
    out: list[Risk] = []
    legs = [b for b in trip.in_order() if b.kind in TRANSPORT]
    for prev, nxt in zip(legs, legs[1:]):
        margin = _margin(prev, nxt)
        if margin is None:
            continue
        minutes = int(margin.total_seconds() // 60)
        separate = (prev.ticket_group != nxt.ticket_group
                    or prev.ticket_group is None)
        if separate and margin < DEPENDENT:
            out.append(Risk(
                kind="unprotected", booking_id=nxt.id,
                sentence=f"{prev.title} and {nxt.title} are separate "
                         f"purchases: if the first runs late, nobody owes you "
                         f"the second. {minutes} minutes to spare",
                margin_minutes=minutes,
                worth=nxt.price or None))
        if margin < TIGHT and not separate:
            out.append(Risk(
                kind="connection", booking_id=nxt.id,
                sentence=f"{minutes} minutes to spare between {prev.title} "
                         f"landing and the last check-in for {nxt.title}",
                margin_minutes=minutes))
    return out


def _appointments(trip: Trip) -> list[Risk]:
    """Commitments reached with minutes to spare. The margin is against the
    latest transport that can deliver the traveller, ground time included."""
    out: list[Risk] = []
    legs = [b for b in trip.in_order() if b.kind in TRANSPORT and b.end]
    for b in trip.in_order():
        if not b.commitment:
            continue
        deliverers = [(l, _margin(l, b)) for l in legs
                      if l.end and l.end <= b.must_arrive_by]
        margins = [(l, m) for l, m in deliverers if m is not None]
        if not margins:
            continue
        leg, margin = max(margins, key=lambda pair: pair[1])
        if timedelta(0) <= margin < TIGHT:
            minutes = int(margin.total_seconds() // 60)
            out.append(Risk(
                kind="connection", booking_id=b.id,
                sentence=f"{b.title} is reachable with {minutes} minutes to "
                         f"spare after {leg.title}",
                margin_minutes=minutes))
    return out


def _breakpoints(trip: Trip) -> list[Risk]:
    """The smallest delay on each leg that starts costing money -- the
    engine's own arithmetic, run over hypothetical delays. One propagate per
    rung, pure functions throughout, nothing stored."""
    out: list[Risk] = []
    for leg in trip.in_order():
        if leg.kind not in TRANSPORT or leg.end is None:
            continue
        for minutes in LADDER:
            supposed = flow.delay(trip, leg.id, minutes)
            impact = propagate(trip, supposed, flow.learned_at(trip, supposed))
            at_stake = impact.do_nothing_cost + impact.at_risk_value
            missed = len(impact.missed)
            if at_stake > 0 or missed:
                what = ("a meeting" if missed and not at_stake else
                        "money" if at_stake and not missed else
                        "money and a meeting")
                out.append(Risk(
                    kind="breakpoint", booking_id=leg.id,
                    sentence=f"{leg.title} absorbs a delay of just under "
                             f"{minutes} minutes; at {minutes} this trip "
                             f"starts losing {what}",
                    breaks_at_minutes=minutes,
                    worth=round(at_stake, 2) or None))
                break
    return out


def assess(trip: Trip) -> list[Risk]:
    """Every thin place in the trip, before anything breaks. Sorted with the
    least slack first, because that is the one to read."""
    found = _connections(trip) + _appointments(trip) + _breakpoints(trip)
    return sorted(found, key=lambda r: (
        r.margin_minutes if r.margin_minutes is not None
        else r.breaks_at_minutes or 10_000))


def as_dicts(risks: list[Risk]) -> list[dict]:
    return [asdict(r) for r in risks]
