"""FastAPI over the engine. `python serve.py`

The engine is untouched and stays stdlib-only; this is a translation layer
between it and a browser. Three things it deliberately does not do:

  * recompute. Every number on the page comes from `propagate` and `generate`,
    the same two calls `cli.py` makes. If the web page and the terminal ever
    disagree, one of them is lying and it will be this one.
  * hide adjustments. `degraded` (a live call fell back to a recording) and
    `shifted` (a recording replayed under a different date) are both surfaced.
    An invisible adjustment is worse than a visible one.
  * pretend to execute. Downstream has no consumer APIs for most of what it
    recommends, so actions are sorted into lanes by who can actually perform
    them, and the "call" lane says so plainly.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from fastapi import FastAPI, HTTPException                    # noqa: E402
from fastapi.responses import FileResponse                    # noqa: E402
from pydantic import BaseModel                                # noqa: E402

import _model                                                 # noqa: E402
import flow                                                   # noqa: E402
from builder import infeasible                                # noqa: E402
import ingest                                                 # noqa: E402
import store as store_module                                  # noqa: E402
from domain import Booking, Kind, Trip                        # noqa: E402
import aerodatabox, duffel, rail                              # noqa: E402,E401
from base import MODE, degraded, shifted                      # noqa: E402
from demo_trip import DEFAULT_BASE, anchor, build_trip, resolve_base   # noqa: E402
from graph import Impact, propagate                           # noqa: E402
from plan import Gap, Lane, generate                          # noqa: E402

STATIC = ROOT / "static"
app = FastAPI(title="downstream")

LANE_KEY = {Lane.AUTO: "auto", Lane.TAP: "tap", Lane.CALL: "call"}

#: A stored trip is reached by an unguessable id and nothing else — the same
#: bargain a shared document link makes. 96 bits of randomness is the whole of
#: the protection, so the id must never appear in a log, a referrer or a URL
#: anybody pastes. Real accounts are the next thing this needs; saying that
#: plainly beats implying an authentication layer that is not here.
OWNER = "anon"


class Paste(BaseModel):
    text: str
    label: str = ""


#: City -> (place code, IANA zone). This is a geocoder in four lines, and
#: naming it as one is the point: a hotel search answers "Milan", which is a
#: fine thing to print and useless to search on, and a check-in time is local to
#: the building. Both have to come from somewhere. A real deployment resolves
#: them; a demo that pretends to is worse than one that shows its table.
PLACES: dict[str, tuple[str, str]] = {
    "Milan": ("MXP", "Europe/Rome"),
    "Zurich": ("ZRH", "Europe/Zurich"),
    "Singapore": ("SIN", "Asia/Singapore"),
    "Florence": ("FLR", "Europe/Rome"),
}


def _zone(name: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except Exception:                                   # noqa: BLE001
        return timezone.utc


def _zone_for(code: str):
    """The zone an airport keeps its clock in, as far as this demo knows.

    Duffel returns the real zone with its reference data and the port prefers
    it; this only has to be right enough to pick the correct calendar day for a
    search, which is why an unknown airport falls back to UTC rather than to the
    zone of whoever is looking at the page.
    """
    return _zone(next((z for c, z in PLACES.values() if c == code.upper()), "UTC"))


def _day(iso: str, zone) -> datetime:
    """An ISO date -> midday in a named zone.

    Midday rather than midnight so that a rounding or offset error cannot move
    the search to the day before, which is the sort of bug that looks like an
    empty result set rather than like a bug.
    """
    try:
        return datetime.fromisoformat(iso).replace(
            hour=12, minute=0, second=0, microsecond=0, tzinfo=zone)
    except ValueError as exc:
        raise HTTPException(400, f"cannot read a date from {iso!r}") from exc


class Leg(BaseModel):
    """One chosen flight, named by the search that found it.

    The offer id alone is not enough to re-resolve: Duffel offers live inside
    the search that produced them. Sending the query back costs three fields and
    means the server never has to hold search results between requests, which is
    the difference between a demo that survives a restart and one that does not.
    """

    origin: str
    destination: str
    on: str
    offer_id: str


class Stay(BaseModel):
    city: str
    country: str = "IT"
    check_in: str
    check_out: str
    hotel_id: str
    code: str = ""


class Selection(BaseModel):
    flights: list[Leg]
    hotels: list[Stay] = []
    label: str = ""


class Cancellation(BaseModel):
    """The button. One leg is not going, and the traveller has just found out.

    Deliberately separate from live status detection rather than dressed up as
    it. Live status is the real signal and reaches about a week either side of
    today; this is what happens when the gate agent has already said so and the
    airline has not published yet. Both end up as the same Disruption, and the
    response says which one this was.
    """

    trip_id: str
    booking_id: str


#: Live flight status covers roughly a week either side of now. Asking about a
#: flight two months out returns nothing useful, and judging a trip that has not
#: started against the current clock produces arithmetic about a day nobody is
#: living yet — the first version of this reported EUR 906 of damage and no
#: deadline at all for a trip six weeks away.
DETECTION_WINDOW = timedelta(days=7)


def _detect(trip: Trip, now: datetime) -> tuple[object | None, str]:
    """Ask each imminent flight whether it is late.

    Most days nothing is wrong, and a product that replans those days is one
    nobody keeps installed. Returning nothing is the normal answer, not a
    failure, so it comes back with a reason rather than as a bare None.
    """
    from base import PortError

    flights = [b for b in trip.in_order() if b.kind is Kind.FLIGHT]
    if not flights:
        return None, "this trip has no flights to watch"

    imminent = [b for b in flights if abs(b.start - now) <= DETECTION_WINDOW]
    if not imminent:
        soonest = min(flights, key=lambda b: abs(b.start - now))
        days = abs((soonest.start - now).days)
        return None, (f"nothing departing within {DETECTION_WINDOW.days} days — "
                      f"the next flight is {days} days away, and live status "
                      f"does not reach that far")

    for booking in imminent:
        code = booking.title.split(" - ")[0].replace(" ", "")
        try:
            found = aerodatabox.disruption(code, booking.start, booking.id)
        except PortError:
            continue          # no recording for this flight; not a disruption
        if found:
            return found, ""
    return None, "every flight checked is running to schedule"


def _base(raw: str) -> date | None:
    try:
        return resolve_base(raw)
    except ValueError as exc:
        raise HTTPException(400, f"cannot read a date from {raw!r}") from exc


def _when(value: datetime | None) -> dict | None:
    """A timestamp formatted in ITS OWN timezone, not the viewer's.

    A browser rendering these itself shows a Zurich arrival in whatever zone the
    laptop is set to — 10:25 becomes 08:25 in the sandbox and 16:25 in
    Singapore. The traveller's itinerary says 10:25, so the page has to as well.
    The ISO value goes along for anything that needs to compute rather than
    display.
    """
    if not value:
        return None
    return {
        "iso": value.isoformat(),
        "t": f"{value:%H:%M}",
        "d": f"{value:%d %b %H:%M}",
        "tz": f"{value:%Z}" or "",
    }


def _action(a) -> dict:
    return {
        "verb": a.verb,
        "label": a.label,
        "lane": LANE_KEY[a.lane],
        "booking_id": a.booking_id,
        "cash_out": a.cash_out,
        "cash_in": a.cash_in,
        "net": a.cash_out - a.cash_in,
        "note": a.note,
        "deadline": _when(a.deadline),
        "price_source": a.price_source,
        # Anything that is not a firm quote is flagged, not just the rail
        # estimate: a converted fare is a real number with a guess applied, and
        # a traveller comparing totals deserves to know which is which.
        "estimated": a.price_source not in ("quoted", ""),
    }


def _plan(p, best: bool) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "tagline": p.tagline,
        "net_cash": p.net_cash,
        "total_damage": p.total_damage,
        "wasted": p.wasted,
        "at_risk": p.at_risk,
        "at_risk_ids": sorted(p.at_risk_ids),
        "delivered": sorted(p.delivered),
        "arrives_at": _when(p.arrives_at),
        "arrives_where": p.arrives_where,
        "best": best,
        "estimated": any(a.price_source not in ("quoted", "") for a in p.actions),
        "tightest": ({"booking_id": p.tightest[0],
                      "minutes": int(p.tightest[1].total_seconds() // 60)}
                     if p.tightest else None),
        "lanes": {key: [_action(a) for a in p.lane(lane)]
                  for lane, key in LANE_KEY.items()},
    }


def _assemble(basis: date | None, trip: Trip | None = None,
              trip_id: str = "") -> dict:
    demo = trip is None
    trip = trip or build_trip(basis)
    now = anchor(basis, 12, 2, 38) if demo else datetime.now(trip.in_order()[0].start.tzinfo)

    quiet = ""
    if demo:
        disruption = aerodatabox.disruption("SQ346", anchor(basis, 11, 9, 0), "sq346")
    else:
        disruption, quiet = _detect(trip, now)
        if disruption is not None:
            # Consequences are judged from the moment the delay is known, which
            # is no earlier than the flight leaving. Evaluating from a "now"
            # before departure asks what a trip nobody has started yet has
            # already cost, and the engine answers — the first version of this
            # reported EUR 906 of damage and no deadline at all, for a flight
            # that had not taken off.
            departure = trip.by_id(disruption.booking_id).start
            now = max(now, departure)
    if disruption is None:
        return {
            "trip_id": trip_id,
            "disrupted": False,
            "quiet_because": quiet,
            "ports": {"mode": MODE, "degraded": list(degraded), "shifted": list(shifted)},
            "model": _model.label(),
            "anchor": {"starts": _when(trip.in_order()[0].start), "now": _when(now),
                       "as_written": basis is None,
                       "default_base": DEFAULT_BASE.isoformat()},
            "bookings": [{"id": b.id, "title": b.title, "provider": b.provider,
                          "starts": _when(b.start), "price": b.price,
                          "currency": b.currency, "ticket_group": b.ticket_group}
                         for b in trip.in_order()],
        }
    offers = (duffel.offers("ZRH", "MXP", anchor(basis, 12, 9, 0), after=disruption.new_end)
              + rail.offers("Zurich HB", "Milano Centrale", anchor(basis, 12, 9, 0),
                            {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}))

    impact = propagate(trip, disruption, now)
    plans = generate(trip, disruption, now, offers)
    return _assessment(trip, disruption, impact, plans, now, basis, trip_id)


def _assessment(trip: Trip, disruption, impact: Impact, plans: list,
                now: datetime, basis: date | None, trip_id: str,
                gap: Gap | None = None, injected: bool = False) -> dict:
    """One shape for every assessment, however the disruption arrived.

    Live status and a traveller pressing "cancelled" produce the same Disruption
    and therefore the same answer; the only difference the page is entitled to
    see is ``injected``, which says which one this was. Two payload shapes would
    let the two paths drift, and the one that drifts is always the one nobody
    demos.
    """
    source = trip.by_id(disruption.booking_id)
    cutoff = impact.next_cutoff

    return {
        "trip_id": trip_id,
        "disrupted": True,
        "ports": {"mode": MODE, "degraded": list(degraded), "shifted": list(shifted)},
        "model": _model.label(),
        "anchor": {
            "starts": _when(trip.in_order()[0].start),
            "now": _when(now),
            "as_written": basis is None,
            "default_base": DEFAULT_BASE.isoformat(),
        },
        "signal": {
            "title": source.title,
            "provider": source.provider,
            "booking_id": source.id,
            "cancelled": disruption.cancelled,
            "injected": injected,
            "delay_minutes": int(disruption.delay(trip).total_seconds() // 60),
            "was": _when(source.end),
            "now_arrives": _when(disruption.new_end),
            "confidence": disruption.confidence,
            "reason": disruption.reason,
        },
        "gap": ({"origin": gap.origin, "destination": gap.destination,
                 "not_before": _when(gap.not_before), "by": _when(gap.by),
                 "hours": round(gap.hours, 1), "for_booking": gap.for_booking}
                if gap else None),
        "impact": {
            "nodes": [{
                "id": n.id,
                "title": n.booking.title,
                "provider": n.booking.provider,
                "severity": n.severity.value,
                "exposure": n.exposure,
                "recoverable": n.recoverable,
                "cutoff": _when(n.cutoff),
                "reason": n.reason,
                "parents": list(n.parents),
                "price": n.booking.price,
                "currency": n.booking.currency,
            } for n in impact.nodes],
            "do_nothing": impact.do_nothing_cost,
            "at_risk": impact.at_risk_value,
            "act_now": impact.act_now_value,
            "broken": len(impact.broken),
            "next_cutoff": ({"at": _when(cutoff[0]),
                             "title": cutoff[1].booking.title,
                             "booking_id": cutoff[1].id,
                             "seconds": int((cutoff[0] - now).total_seconds())}
                            if cutoff else None),
        },
        "plans": [_plan(p, i == 0) for i, p in enumerate(plans)],
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "ports_mode": MODE,
        "degraded": list(degraded),
        "shifted": list(shifted),
        "model": _model.label(),
        "storage": "postgres" if store_module.store().durable else "memory (lost on restart)",
        "fixtures": sorted(p.name for p in (ROOT / "fixtures").glob("*")),
    }


@app.post("/api/trip")
def add_trip(body: Paste) -> dict:
    """Pasted or forwarded confirmations -> a stored, checked trip.

    Problems and warnings come back with the trip rather than blocking it: a
    traveller with eleven good bookings and one unreadable one should see the
    eleven and be told about the twelfth, not be handed an error.
    """
    if len(body.text.strip()) < 20:
        raise HTTPException(400, "paste the confirmation emails, not just a subject line")

    result = ingest.extract(body.text, _model.get_model())
    if not result.trip.bookings:
        raise HTTPException(422, {"problems": result.problems})

    saved = store_module.store().save(
        OWNER, {"bookings": ingest.to_dicts(result.trip.bookings)},
        body.label or "Untitled trip")
    return {
        "trip_id": saved.id,
        "label": saved.label,
        "durable": store_module.store().durable,
        "bookings": [{"title": b.title, "provider": b.provider,
                      "starts": _when(b.start), "price": b.price,
                      "currency": b.currency, "ticket_group": b.ticket_group,
                      "source": result.sources.get(b.id, "")}
                     for b in result.trip.in_order()],
        "problems": result.problems,
        "warnings": result.warnings,
    }


def _offer(o) -> dict:
    return {
        "id": o.id,
        "mode": o.mode,
        "carrier": o.carrier,
        "label": o.label,
        "origin": o.origin,
        "destination": o.destination,
        "depart": _when(o.depart),
        "arrive": _when(o.arrive),
        "overnight": o.arrive.date() > o.depart.date(),
        "minutes": int((o.arrive - o.depart).total_seconds() // 60),
        "price": o.price,
        "currency": o.currency,
        "price_source": o.price_source,
        "quoted": o.quoted,
        "estimated": o.price_source != "quoted",
        "refundable": o.refundable,
        "change_fee": o.change_fee,
    }


def _stay(h: dict) -> dict:
    return {
        "id": h["id"],
        "name": h["name"],
        "city": h["city"],
        "code": h.get("city_code") or "",
        "address": h.get("address") or "",
        "stars": h.get("stars"),
        "nights": h["nights"],
        "check_in": _when(h["check_in"]),
        "held_until": _when(h["arrival_guarantee"]),
        "price": h["price"],
        "currency": h["currency"],
    }


@app.get("/api/search/flights")
def search_flights(origin: str, destination: str, on: str, after: str = "") -> dict:
    """Real schedules and real fares, from an account that cannot take money.

    That is the right shape for what is being shown: the hard part was never the
    card, it was knowing what to buy and by when.
    """
    from base import PortError

    zone = _zone_for(origin)
    not_before = datetime.fromisoformat(after) if after else None
    try:
        found = flow.search_flights(origin.upper(), destination.upper(),
                                    _day(on, zone), after=not_before)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"origin": origin.upper(), "destination": destination.upper(),
            "on": on, "offers": [_offer(o) for o in found]}


@app.get("/api/search/hotels")
def search_hotels(city: str, country: str = "IT", check_in: str = "",
                  check_out: str = "") -> dict:
    from base import PortError

    code, zone_name = PLACES.get(city, ("", "UTC"))
    zone = _zone(zone_name)
    try:
        found = flow.search_hotels(city, country, _day(check_in, zone),
                                   _day(check_out, zone), code=code)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"city": city, "code": code, "stays": [_stay(h) for h in found]}


@app.post("/api/select")
def select(body: Selection) -> dict:
    """Chosen offers -> a stored trip, plus every reason it could not be taken.

    Problems come back WITH the trip rather than instead of it. A selection with
    an unreachable room is still the selection somebody made, and showing them
    the itinerary next to the reason it does not work is the only version of
    this that teaches anything.
    """
    from base import PortError

    if not body.flights:
        raise HTTPException(400, "pick at least one flight")

    picked = []
    try:
        for leg in body.flights:
            zone = _zone_for(leg.origin)
            found = flow.search_flights(leg.origin.upper(),
                                        leg.destination.upper(),
                                        _day(leg.on, zone))
            offer = next((o for o in found if o.id == leg.offer_id), None)
            if offer is None:
                raise HTTPException(
                    409, f"offer {leg.offer_id} is no longer in that search — "
                         "search again and pick from the current fares")
            picked.append(offer)

        stays = []
        for want in body.hotels:
            code, zone_name = PLACES.get(want.city, (want.code, "UTC"))
            zone = _zone(zone_name)
            found = flow.search_hotels(want.city, want.country,
                                       _day(want.check_in, zone),
                                       _day(want.check_out, zone),
                                       code=want.code or code)
            hotel = next((h for h in found if h["id"] == want.hotel_id), None)
            if hotel is None:
                raise HTTPException(409, f"stay {want.hotel_id} is no longer available")
            stays.append(hotel)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc

    assembled, _ = flow.select(picked, stays)
    saved = store_module.store().save(
        OWNER, {"bookings": ingest.to_dicts(assembled.bookings)},
        body.label or "Booked here")

    # Answer with the trip AS IT READS BACK, not as it was assembled. Booking
    # ids are derived from the title when a trip is loaded -- a fresh extraction
    # from a confirmation email has no ids to store, so there is nothing else
    # they could be -- which means the ids handed out here would otherwise name
    # bookings that no later request could find. Cancelling a leg answered "no
    # booking with that id on this trip" for a leg the client had just been
    # shown. Round-tripping in place also means any field that does not survive
    # storage is visible in this response instead of during the demo.
    trip = _load(saved.id)
    problems = infeasible(trip)

    return {
        "trip_id": saved.id,
        "label": saved.label,
        "durable": store_module.store().durable,
        "problems": problems,
        "total": round(sum(b.price for b in trip.bookings), 2),
        "bookings": [{"id": b.id, "title": b.title, "provider": b.provider,
                      "kind": b.kind.value, "where": b.where,
                      "starts": _when(b.start), "ends": _when(b.end),
                      "must_arrive_by": _when(b.must_arrive_by),
                      "price": b.price, "currency": b.currency,
                      "ticket_group": b.ticket_group,
                      "policy": b.policy.source}
                     for b in trip.in_order()],
    }


@app.post("/api/cancel")
def cancel(body: Cancellation) -> dict:
    """The button. Cancel one leg and answer the whole question from live data.

    Nothing here decides anything: the gap is derived in plan.py from where the
    traveller now stands, the replacements come from the same provider that sold
    the original, and the ranking is the same arithmetic the terminal runs.
    """
    from base import PortError

    trip = _load(body.trip_id)
    try:
        trip.by_id(body.booking_id)
    except KeyError as exc:
        raise HTTPException(404, "no booking with that id on this trip") from exc

    try:
        recovery = flow.replan(trip, body.booking_id)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc

    payload = _assessment(trip, recovery.disruption, recovery.impact,
                          recovery.plans, recovery.disruption.new_end, None,
                          body.trip_id, gap=recovery.gap, injected=True)
    payload["searched"] = [_offer(o) for o in recovery.offers]
    payload["saved"] = recovery.saved
    return payload


def _load(trip_id: str) -> Trip:
    saved = store_module.store().get(trip_id)
    if saved is None:
        raise HTTPException(404, "no trip with that id")
    bookings, problems, _sources = ingest.to_bookings(saved.payload)
    if not bookings:
        raise HTTPException(422, {"problems": problems})
    return Trip(bookings)


@app.get("/api/state")
def state(base: str = "today", trip: str = "") -> dict:
    """The whole assessment. Same two engine calls the CLI makes.

    Without ``trip`` this answers about the built-in scenario, which is what the
    demo and the tests use. With one, it answers about a real stored itinerary.
    """
    from base import PortError

    loaded = _load(trip) if trip else None

    try:
        return _assemble(_base(base), loaded, trip)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/book")
def book() -> FileResponse:
    """Build a trip from live inventory, then break it.

    Kept as a second page rather than folded into the first. The scripted delay
    on `/` is the argument -- one late flight, eleven bookings, EUR 282 -- and it
    has to keep running from the fixtures exactly as it does today. This one is
    the same engine with nothing scripted: the trip is whatever somebody picks
    and the disruption is whichever leg they cancel.
    """
    return FileResponse(STATIC / "book.html")


def _free_port(start: int, tries: int = 12) -> int:
    import socket

    for offset in range(tries):
        candidate = start + offset
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise SystemExit(f"no free port in {start}..{start + tries - 1}")


if __name__ == "__main__":
    import uvicorn

    hosted = bool(os.environ.get("PORT"))
    port = int(os.environ["PORT"]) if hosted else _free_port(8000)
    host = "0.0.0.0" if hosted else "127.0.0.1"
    if not hosted:
        print(f"\n  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
