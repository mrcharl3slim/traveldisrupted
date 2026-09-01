"""Where the traveller is, at every moment, derived from the itinerary.

The itinerary already says it: at home before the first leg, in the air or on
the rails during a leg, at the destination from arrival until the next leg
departs, and home again after the return. Nothing here consults the transit
table for city-to-city questions -- `domain.transit` knows sixteen LOCAL hops
(airport to station to hotel), and an absent row there means "no ground link
we can time", never "no aircraft crosses this distance". Movement between
cities comes from the trip's own transport legs, exactly as
`graph.ReachModel.took` counts it: a booking with both an origin and a
destination moves the traveller; hotels and appointments never do.

Two honesty rules, load-bearing:

* An unknown place code resolves to city "" and every check degrades to
  today's behaviour. Nothing is refused out of ignorance -- the same principle
  as `builder._known`.
* The HOME ANCHOR IS SOFT. A trip with no return leg leaves the traveller
  presumed at the last destination indefinitely, and `open_ended` says so; it
  blocks nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import places
from domain import Kind, Trip

#: The gap an appointment needs on each side of any neighbouring event in the
#: same city. Two hours is the user's rule, stated as policy: land at 16:00
#: and a 17:00 meeting gets a warning the traveller can approve past --
#: flight-to-flight connections keep their own MIN_CONNECTION rule and are
#: not judged here.
MIN_GAP = timedelta(hours=2)


@dataclass(frozen=True)
class Interval:
    """One stretch of the traveller's life: a city, from when, to when.

    ``frm``/``to`` are aware datetimes; None means unbounded on that side.
    ``city`` is "" while in transit or when the place code is unknown.
    ``source`` names what put them there: a booking id, "home", or "transit".
    """

    city: str
    place: str
    frm: datetime | None
    to: datetime | None
    source: str


def _city(code: str) -> str:
    found = places.by_code(code or "")
    return found.hotel_city if found else ""


def timeline(trip: Trip, home: str = "") -> list[Interval]:
    """The traveller's location intervals, in order.

    With no home set, presence starts at the first leg's ORIGIN city -- they
    must be there to depart -- so wrong-city checks work without a profile.
    With no transport legs at all, the traveller is simply at home (or
    nowhere we can claim, which is an empty timeline and no opinions).
    """
    legs = [b for b in trip.in_order()
            if b.start and b.origin and b.destination]
    home_city = _city(home)
    if not legs:
        return ([Interval(home_city, home, None, None, "home")]
                if home_city else [])

    out: list[Interval] = []
    first = legs[0]
    start_city = home_city or _city(first.origin)
    out.append(Interval(start_city, home or (first.origin or ""),
                        None, first.start, "home" if home_city else first.id))
    for i, leg in enumerate(legs):
        arrived = leg.end or leg.start
        out.append(Interval("", "", leg.start, arrived, "transit"))
        nxt = legs[i + 1] if i + 1 < len(legs) else None
        until = nxt.start if nxt is not None else None
        if until is not None and until < arrived:
            # An out-of-order parse must not mint a negative interval;
            # ingest.validate already warns about the itinerary itself.
            until = arrived
        out.append(Interval(_city(leg.destination), leg.destination or "",
                            arrived, until, leg.id))
    return out


def open_ended(tl: list[Interval], home: str = "") -> bool:
    """True when the trip never brings the traveller home.

    The presumption the last interval rests on: no return leg means "still at
    the last destination", said out loud rather than silently believed.
    """
    if not tl:
        return False
    last = tl[-1]
    if last.to is not None or last.source == "home":
        return False
    return not home or last.city != _city(home)


def at(tl: list[Interval], when: datetime) -> Interval | None:
    """The interval covering ``when`` -- possibly a transit one."""
    for iv in tl:
        if (iv.frm is None or iv.frm <= when) and (iv.to is None or when < iv.to):
            return iv
    return None


def where_on(tl: list[Interval], day: date, zone) -> list[Interval]:
    """Every interval touching that local day. A flight day has two cities."""
    if day is None or zone is None:
        return []
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    out = []
    for iv in tl:
        if (iv.frm is None or iv.frm < day_end) and (iv.to is None or day_start < iv.to):
            out.append(iv)
    return out


def valid_here(tl: list[Interval], city: str,
               start: datetime, end: datetime) -> bool:
    """Can the traveller be in ``city`` for the whole of [start, end]?

    True when any same-city interval overlaps the span -- and true whenever
    the question cannot honestly be asked (no timeline, unknown city), because
    a refusal invented from ignorance is worse than no check.
    """
    if not tl or not city:
        return True
    for iv in tl:
        if iv.city != city:
            continue
        if (iv.frm is None or iv.frm < end) and (iv.to is None or start < iv.to):
            return True
    return False


def tight(trip: Trip, booking) -> list[dict]:
    """Neighbours closer than MIN_GAP to this appointment, in its own city.

    Judged around the appointment only: flights against flights keep
    MIN_CONNECTION in `builder.infeasible`. Overlapping pairs are skipped --
    those are already blocking clashes, not warnings. For a flight before the
    appointment the gap runs from its arrival; for one after, it runs to its
    `must_arrive_by`, because check-in closes long before wheels-up.
    """
    out: list[dict] = []
    b_start, b_end = booking.start, booking.end or booking.start
    b_city = _city(booking.origin or "")
    if not b_start or not b_city:
        return out
    for other in trip.in_order():
        if other.id == booking.id or other.kind is Kind.LODGING or not other.start:
            continue
        o_start, o_end = other.start, other.end or other.start
        if o_start < b_end and b_start < o_end:
            continue                      # an overlap is a clash, not a warning
        if o_end <= b_start:
            city = _city(other.destination or other.origin or "")
            gap = b_start - o_end
            if city == b_city and gap < MIN_GAP:
                minutes = int(gap.total_seconds() // 60)
                out.append({
                    "gap_minutes": minutes, "neighbour": other.title,
                    "message": f"only {minutes} minutes after {other.title} "
                               f"— I'd leave at least 2 hours"})
        else:
            city = _city(other.origin or "")
            cutoff = other.must_arrive_by if other.destination else o_start
            gap = cutoff - b_end
            if city == b_city and gap < MIN_GAP:
                minutes = max(0, int(gap.total_seconds() // 60))
                out.append({
                    "gap_minutes": minutes, "neighbour": other.title,
                    "message": f"only {minutes} minutes before {other.title} "
                               f"— I'd leave at least 2 hours"})
    return out
