"""The 11-17 October trip, as structured facts rather than as a story.

Every string here came off a real-looking confirmation; every Window is a fare
rule resolved to a timestamp. Nothing in this file states a consequence. There
is no "critical", no "EUR 282", no plan -- those are the engine's job, and if
they ever appear in this file the demo has quietly become a mockup again.

The structural flaw is expressed in exactly one place: SQ 346 carries
ticket_group "SQ-9F2K" and LX 1608 carries "LX-4T81". Two contracts. Nobody
owes the traveller a connection.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

from domain import Booking, Kind, Trip
from policy import Policy, Window

CEST = timezone(timedelta(hours=2))
SGT = timezone(timedelta(hours=8))


def dt(day: int, hh: int, mm: int, tz=CEST, month: int = 10) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=tz)


import money as _fx


def _eur(amount: float) -> float:
    """A EUR quote from a confirmation email, in the engine's home currency.

    The same boundary a live quote crosses: fixed rate, declared in money.py,
    and every figure downstream -- the S$423 headline included -- is this
    conversion applied and then computed, never typed.
    """
    return _fx.to_home(amount, "EUR")[0]


TRIP = Trip([
    Booking(
        id="sq346", kind=Kind.FLIGHT, provider="Singapore Airlines",
        title="SQ 346 - Singapore to Zurich",
        start=dt(11, 23, 55, SGT), end=dt(12, 6, 35),
        origin="SIN", destination="ZRH",
        price=1140.0, currency="SGD", ticket_group="SQ-9F2K",
        policy=Policy(source="Economy Value - change fee S$150"),
    ),
    Booking(
        id="lx1608", kind=Kind.FLIGHT, provider="SWISS",
        title="LX 1608 - Zurich to Milan Malpensa",
        start=dt(12, 8, 20), end=dt(12, 9, 35),
        origin="ZRH", destination="MXP",
        price=_eur(142.0), currency="SGD", price_source="converted", ticket_group="LX-4T81",
        policy=Policy(
            source="Economy Classic - no-show forfeits fare",
            windows=[Window(closes=dt(12, 8, 20), refund=_eur(38.0),
                            label="taxes refundable if cancelled before departure")],
        ),
    ),
    Booking(
        id="transfer", kind=Kind.TRANSFER, provider="Welcome Pickups",
        title="Malpensa to hotel private transfer",
        start=dt(12, 10, 0), end=dt(12, 10, 50),
        origin="MXP", destination="MILAN",
        price=_eur(48.0), currency="SGD", price_source="converted",
        policy=Policy(
            source="Free change up to 4 hours before pickup",
            # 4 h before a 10:00 pickup. Resolved once, here, where the pickup
            # time is in hand -- the whole point of policy.py.
            windows=[Window(closes=dt(12, 6, 0), refund=_eur(48.0),
                            label="free change or cancel until 06:00")],
        ),
    ),
    Booking(
        id="hotel", kind=Kind.LODGING, provider="Booking.com",
        title="Hotel Le Marais Milano - 3 nights",
        start=dt(12, 14, 0), end=dt(15, 11, 0), origin="MILAN",
        price=_eur(624.0), currency="SGD", price_source="converted",
        hard_deadline=dt(12, 22, 0),
        mitigation="arrival guarantee lapses at 22:00 - notify the property",
        policy=Policy(source="Non-refundable rate, arrival guarantee to 22:00"),
    ),
    Booking(
        id="lastsupper", kind=Kind.ACTIVITY, provider="GetYourGuide",
        title="The Last Supper - timed entry",
        start=dt(12, 16, 0), end=dt(12, 16, 15), origin="SMG",
        price=_eur(92.0), currency="SGD", price_source="converted", fixed_slot=True,
        policy=Policy(
            source="Non-refundable. Date change subject to slots, EUR 18 fee",
            windows=[Window(closes=dt(12, 16, 0), refund=_eur(92.0), fee=_eur(18.0),
                            label="date change, EUR 18, subject to availability")],
        ),
    ),
    Booking(
        id="dinner", kind=Kind.ACTIVITY, provider="Trattoria Milanese",
        title="Dinner - Trattoria Milanese",
        start=dt(12, 20, 0), end=dt(12, 22, 0), origin="MILAN",
        price=0.0,
        policy=Policy(source="Free cancellation, nothing prepaid"),
    ),
    Booking(
        id="como", kind=Kind.ACTIVITY, provider="GetYourGuide",
        title="Bellagio and Lake Como day tour",
        start=dt(13, 9, 15), end=dt(13, 18, 0), origin="MILAN",
        price=_eur(128.0), currency="SGD", price_source="converted",
        policy=Policy(
            source="Free cancellation to 12 Oct 09:15",
            windows=[Window(closes=dt(12, 9, 15), refund=_eur(128.0),
                            label="free cancellation until 09:15 tomorrow")],
        ),
    ),
    Booking(
        id="scala", kind=Kind.ACTIVITY, provider="Teatro alla Scala",
        title="La Scala - Rigoletto",
        start=dt(13, 19, 30), end=dt(13, 22, 15), origin="MILAN",
        price=_eur(180.0), currency="SGD", price_source="converted", fixed_slot=True,
        policy=Policy(source="Non-refundable, seat-specific"),
    ),
    Booking(
        id="fr9520", kind=Kind.RAIL, provider="Trenitalia",
        title="Frecciarossa 9520 - Milano C.le to Firenze",
        start=dt(15, 11, 20), end=dt(15, 13, 15),
        origin="MILANO_C", destination="FLR",
        price=_eur(89.0), currency="SGD", price_source="converted",
        policy=Policy(source="Super Economy, non-refundable"),
    ),
    Booking(
        id="palazzo", kind=Kind.LODGING, provider="Booking.com",
        title="Palazzo Vecchietti - 2 nights",
        start=dt(15, 15, 0), end=dt(17, 11, 0), origin="FLR",
        price=_eur(430.0), currency="SGD", price_source="converted",
        policy=Policy(
            source="Flexible rate, free cancellation to 14 Oct",
            windows=[Window(closes=dt(14, 23, 59), refund=_eur(430.0),
                            label="free cancellation until 14 Oct")],
        ),
    ),
    Booking(
        id="fr9508", kind=Kind.RAIL, provider="Trenitalia",
        title="Frecciarossa 9508 - Firenze to Milano C.le",
        start=dt(17, 9, 5), end=dt(17, 11, 0),
        origin="FLR", destination="MILANO_C",
        price=_eur(79.0), currency="SGD", price_source="converted",
        policy=Policy(source="Non-refundable"),
    ),
    Booking(
        id="sq345", kind=Kind.FLIGHT, provider="Singapore Airlines",
        title="SQ 345 - Zurich to Singapore",
        start=dt(17, 13, 40), end=dt(18, 7, 25, SGT),
        origin="ZRH", destination="SIN",
        price=0.0, currency="SGD", ticket_group="SQ-9F2K",
        policy=Policy(source="Return leg of the outbound ticket"),
    ),
])


# --------------------------------------------------------------------------
# anchoring the trip to run time
# --------------------------------------------------------------------------

#: The date TRIP above is written against -- day one, departure from Singapore.
DEFAULT_BASE = date(2026, 10, 11)


def shift_trip(trip: Trip, days: int) -> Trip:
    """The same trip, every date moved by ``days``.

    Written as a shift rather than as a parameterised builder on purpose. The
    literal above carries structure that matters and is easy to break by
    retyping: two ticket groups on the Singapore and Zurich legs so that nobody
    owns the connection, and fare windows resolved against each booking's own
    times. Moving the calendar cannot disturb any of it -- every interval,
    every gap and every deadline keeps its exact relationship to the others.

    Live flight status only covers about a week either side of today, so a trip
    pinned to October cannot be demonstrated live in September. This is what
    makes "live detection" true rather than aspirational.
    """
    if not days:
        return trip
    delta = timedelta(days=days)

    def move(value):
        """Every datetime moves; everything else is passed through untouched.

        Deliberately field-agnostic. Naming the fields to shift means a datetime
        added later -- ``hard_deadline`` was exactly this -- silently stays in
        the old calendar, and the failure is not a crash but a quietly wrong
        answer: a hotel that reports itself safe because its arrival guarantee
        is six weeks after the flight that was supposed to reach it.
        """
        return value + delta if isinstance(value, datetime) else value

    moved = []
    for booking in trip.bookings:
        fields = {f.name: move(getattr(booking, f.name))
                  for f in dataclasses.fields(booking)}
        policy = fields.get("policy")
        if policy is not None and getattr(policy, "windows", None):
            fields["policy"] = dataclasses.replace(policy, windows=[
                dataclasses.replace(w, closes=move(w.closes)) for w in policy.windows
            ])
        moved.append(dataclasses.replace(booking, **fields))
    return Trip(moved)


def build_trip(base: date | None = None) -> Trip:
    """The demo trip, anchored so day one falls on ``base``."""
    if base is None:
        return TRIP
    return shift_trip(TRIP, (base - DEFAULT_BASE).days)


def as_written(day: int) -> date:
    """The calendar date trip-day ``day`` falls on in the literal above.

    The scenario's fixtures were captured against these dates, so this is what a
    port is told to prefer when a route has been recorded more than once. Pinned
    to the literal rather than to run time on purpose: the demo's figures are
    asserted by tests and shown to judges, and they must not move because the
    trip is being anchored to a different week.
    """
    return dt(day, 12, 0).date()


def anchor(base: date | None, day: int, hh: int, mm: int, tz=CEST) -> datetime:
    """A datetime on trip-day ``day`` (11-17), under the same shift as build_trip."""
    offset = 0 if base is None else (base - DEFAULT_BASE).days
    return dt(day, hh, mm, tz) + timedelta(days=offset)


def resolve_base(raw: str | None = None) -> date | None:
    """One place that turns a request into an anchor date.

        ""  /  "today"   -> today, which is the default everywhere
        "written"        -> the trip exactly as written, October 2026
        "+3" / "-1"      -> days from today
        "2026-09-07"     -> that date

    Today is the default because live flight status only covers about a week
    either side of now: a scenario pinned to October cannot be demonstrated
    live in September, so "as written" is the special case rather than the norm.
    Returning None for it keeps the original literal reachable for the tests,
    which assert against dates a human can check by hand.
    """
    raw = (raw or "").strip().lower()
    if raw in ("written", "as-written", "aswritten"):
        return None
    if raw in ("", "today", "now"):
        return date.today()
    if raw[0] in "+-":
        return date.today() + timedelta(days=int(raw))
    return date.fromisoformat(raw)
