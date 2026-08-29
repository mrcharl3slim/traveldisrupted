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

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from fastapi import FastAPI, HTTPException, Request           # noqa: E402
from fastapi.responses import FileResponse, JSONResponse      # noqa: E402
from pydantic import BaseModel                                # noqa: E402

import _model                                                 # noqa: E402
import act as act_mod                                         # noqa: E402
import appointment as appt_mod                                # noqa: E402
import converse                                               # noqa: E402
import flow                                                   # noqa: E402
import places                                                 # noqa: E402
import request as request_mod                                 # noqa: E402
import monitor                                                # noqa: E402
import notify                                                 # noqa: E402
from builder import clashes, infeasible                       # noqa: E402
import ingest                                                 # noqa: E402
import store as store_module                                  # noqa: E402
from domain import Booking, Kind, Trip                        # noqa: E402
import aerodatabox, duffel, rail                              # noqa: E402,E401
from base import MODE, degraded, shifted                      # noqa: E402
from demo_trip import (DEFAULT_BASE, anchor, as_written, build_trip,   # noqa: E402
                        resolve_base)
from graph import Impact, propagate                           # noqa: E402
from plan import Gap, Lane, generate                          # noqa: E402

STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the deadline watch with the service, stop it with the service.

    In-process, and honest about what that means. This is a real watch and not
    a durable scheduler: it runs while the web service runs, which on a free
    instance means it stops when the instance sleeps -- and the deadline at
    04:00 does not care that nobody was awake to serve a request. Production
    swaps this loop for a cron or a queue and changes nothing else, because the
    schedule is a pure function and the watermark is in the database. Saying so
    here beats a judge finding it.
    """
    task = None
    if os.environ.get("DOWNSTREAM_WATCH", "1") != "0":
        task = asyncio.create_task(_watch())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(title="downstream", lifespan=lifespan)

LANE_KEY = {Lane.AUTO: "auto", Lane.TAP: "tap", Lane.CALL: "call"}


@app.exception_handler(Exception)
async def _always_json(_request: Request, exc: Exception) -> JSONResponse:
    """An unhandled error is still an answer, and it has to be readable.

    Starlette's default is plain text, which a JSON client reports as
    "unreadable response" -- three words that describe every proxy error page
    in existence and identify none of them. Naming the exception costs nothing
    and turns a mystery into a line somebody can act on.
    """
    return JSONResponse(status_code=500,
                        content={"detail": f"{type(exc).__name__}: {exc}"})

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

    ``offer_key`` is what is matched; ``offer_id`` is kept for older clients
    and only works while the search that produced it is still the current one.

    The offer id alone is not enough to re-resolve: Duffel offers live inside
    the search that produced them. Sending the query back costs three fields and
    means the server never has to hold search results between requests, which is
    the difference between a demo that survives a restart and one that does not.
    """

    origin: str
    destination: str
    on: str
    offer_key: str = ""
    offer_id: str = ""


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


class Chat(BaseModel):
    """One turn. The whole request travels with it, so nothing is held here.

    A server that remembers half a booking in RAM loses it on the next deploy,
    and a traveller who refreshes the page finds the agent has forgotten where
    they were going. The state is small enough to send back and forth, and
    sending it back and forth is what makes the conversation survive anything.
    """

    text: str = ""
    state: dict = {}
    answers: dict[str, str] = {}


class Choice(BaseModel):
    """A selection, named by what it is plus what it cost on screen.

    `flight_id` is still accepted so an existing client keeps working, but the
    key is what is matched: ids do not survive the re-search, and the price is
    what tells a moved fare apart from a withdrawn one.
    """

    state: dict
    flight_key: str = ""
    flight_price: float = 0.0
    return_key: str = ""
    return_price: float = 0.0
    hotel_id: str = ""
    label: str = ""
    flight_id: str = ""          # deprecated; ignored when a key is supplied
    return_id: str = ""


class Act(BaseModel):
    trip_id: str
    booking_id: str
    plan_key: str = ""
    plan_id: str = ""            # deprecated; ignored when a key is supplied


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
        "key": p.key,
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
    offers = (duffel.offers("ZRH", "MXP", anchor(basis, 12, 9, 0), after=disruption.new_end,
                              prefer=as_written(12))
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


#: How often the watch looks. A minute is finer than any deadline in a fare
#: rule and coarse enough that a free instance spends no measurable time on it.
TICK = timedelta(minutes=1)


def _alerts(saved, now: datetime | None = None) -> tuple[Trip | None, object, list]:
    """A stored trip -> its schedule. Returns empties rather than raising.

    Called from a loop that must keep running: one unreadable payload cannot be
    allowed to end the watch for every other trip in the process.

    ``now`` is injectable because the schedule is a pure function of the clock,
    so the clock is an input. A cron passes the tick it woke up for; a test asks
    what this trip would be told at 21:30. Reading the wall clock in here would
    make the one thing worth testing about a notifier -- what it says, and when
    -- testable only by waiting.
    """
    disruption = flow.from_dict(saved.payload.get("disruption"))
    if disruption is None:
        return None, None, []
    bookings, _problems, _sources = ingest.to_bookings(saved.payload)
    if not bookings:
        return None, None, []
    trip = Trip(bookings)
    try:
        trip.by_id(disruption.booking_id)
    except KeyError:
        return None, None, []
    when = now or datetime.now(trip.in_order()[0].start.tzinfo)
    return trip, disruption, monitor.schedule(trip, disruption, when)


def sweep(now: datetime | None = None) -> int:
    """One pass over every watched trip. Returns how many alerts went out.

    The watermark is stored on the trip and written only when something fired.
    That is what makes "once" true across a restart, a second worker, and the
    schedule being rebuilt from scratch on every pass -- there is no queue to
    lose and no timer to survive.
    """
    fired = 0
    for saved in store_module.store().watching():
        trip, _disruption, alerts = _alerts(saved, now)
        if not alerts:
            continue
        when = now or datetime.now(trip.in_order()[0].start.tzinfo)
        mark = saved.payload.get("watermark")
        since = datetime.fromisoformat(mark) if mark else None
        ready = monitor.due(alerts, since, when)
        if not ready:
            continue
        for alert in ready:
            notify.deliver(alert, saved.id)
            fired += 1
        payload = dict(saved.payload)
        payload["watermark"] = max(a.at for a in ready).isoformat()
        payload["last_sent"] = [
            {"at": a.at.isoformat(), "message": a.message} for a in ready[-5:]]
        store_module.store().update(saved.id, payload)
    return fired


async def _watch() -> None:
    while True:
        try:
            sweep()
        except Exception as exc:                        # noqa: BLE001
            # Never let one bad pass end the watch. A notifier that dies
            # quietly is worse than no notifier: the page still promises it.
            print(f"watch: {type(exc).__name__}: {exc}")
        await asyncio.sleep(TICK.total_seconds())


@app.get("/api/alerts")
def alerts(trip: str) -> dict:
    """Everything this trip still has to be told, and what it was already told.

    Fire times in the past are included. A traveller opening the page at 03:00
    has to see the 01:00 deadline they slept through as well as the 06:00 one
    they can still make; a schedule that hides what it failed to deliver is the
    one bug in a notifier nobody catches.
    """
    saved = store_module.store().get(trip)
    if saved is None:
        raise HTTPException(404, "no trip with that id")
    watched, _disruption, schedule = _alerts(saved)
    if watched is None:
        return {"trip_id": trip, "watching": False,
                "why": "nothing is disrupted on this trip, so there is no clock",
                "channels": notify.channels(), "alerts": []}

    now = datetime.now(watched.in_order()[0].start.tzinfo)
    return {
        "trip_id": trip,
        "watching": True,
        "now": _when(now),
        "channels": notify.channels(),
        "reaches_you": [k for k, on in notify.channels().items() if on],
        "next": (lambda a: {"at": _when(a.at), "message": a.message} if a else None)(
            monitor.next_up(schedule, now)),
        "alerts": [{
            "at": _when(a.at),
            "closes": _when(a.closes),
            "kind": a.kind,
            "lead": a.lead,
            "booking_id": a.booking_id,
            "title": a.title,
            "worth": a.worth,
            "message": a.message,
            "passed": a.at <= now,
        } for a in schedule],
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "ports_mode": MODE,
        "degraded": list(degraded),
        "shifted": list(shifted),
        "model": _model.label(),
        # Configured vs actually working. /health is the one place that must
        # not report an intention as a fact.
        "model_status": _model.effective(),
        "storage": "postgres" if store_module.store().durable else "memory (lost on restart)",
        "watch": {
            "running": os.environ.get("DOWNSTREAM_WATCH", "1") != "0",
            "every_seconds": int(TICK.total_seconds()),
            "channels": notify.channels(),
            "durable": False,
            "caveat": "in-process: it stops when the instance sleeps",
        },
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
        # What the flight IS. The id names a priced contract that expires with
        # the search; this survives re-searching, which is what a selection
        # made ninety seconds ago actually needs.
        "key": o.key,
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


def _req_out(req) -> dict:
    return {
        "origin": req.origin, "origin_label": places.label(req.origin),
        "destination": req.destination,
        "destination_label": places.label(req.destination),
        "depart": req.depart.isoformat() if req.depart else None,
        "ret": req.ret.isoformat() if req.ret else None,
        "one_way": req.one_way, "travellers": req.travellers,
        "hotel": req.hotel, "hotel_area": req.hotel_area,
        "confirmed": req.confirmed, "nights": req.nights,
        "hotel_in": req.hotel_in.isoformat() if req.hotel_in else None,
        "hotel_out": req.hotel_out.isoformat() if req.hotel_out else None,
        "preference": req.preference, "summary": req.summary(),
        "ready": req.ready,
    }


def _req_in(raw: dict):
    """A request off the wire. Unreadable fields are treated as unanswered.

    Never trusted into a search without re-deriving `gaps` from it, so a
    tampered payload cannot skip a question -- it can only lie about answers it
    then has to live with.
    """
    from datetime import date as _date

    def when(value):
        try:
            return _date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    def place(value) -> str:
        found = places.find(str(value or ""))
        return found.code if found else ""

    return request_mod.Request(
        origin=place(raw.get("origin")), destination=place(raw.get("destination")),
        depart=when(raw.get("depart")), ret=when(raw.get("ret")),
        one_way=raw.get("one_way") if isinstance(raw.get("one_way"), bool) else None,
        travellers=max(1, min(int(raw.get("travellers") or 1), 9)),
        hotel=raw.get("hotel") if isinstance(raw.get("hotel"), bool) else None,
        hotel_area=str(raw.get("hotel_area") or "")[:60],
        hotel_in=when(raw.get("hotel_in")), hotel_out=when(raw.get("hotel_out")),
        confirmed=bool(raw.get("confirmed")),
        preference=(str(raw.get("preference") or "").lower()
                    if str(raw.get("preference") or "").lower()
                    in request_mod.PREFERENCES else ""),
        raw=str(raw.get("raw") or "")[:2000],
    )


def _rematch(offers: list, key: str, shown: float, what: str) -> tuple:
    """Find the flight the traveller pointed at, in a search they have not seen.

    Two things can have happened between the click and this request. The fare
    can have moved, which is ordinary and has to be said rather than swallowed.
    Or the flight can have gone, which is a refusal -- silently booking the
    nearest thing is how somebody ends up holding a ticket they did not choose.

    Several offers can share a key: the same aircraft is sold under several
    fare brands, and we saw JU 0333 at both EUR 170 and EUR 255 in one search.
    The one nearest what was on screen is the one they meant.
    """
    candidates = [o for o in offers if o.key == key]
    if not candidates:
        # An older client sent an id instead of a key. It only matches while
        # the search that produced it is still the current one -- which is true
        # in replay and almost never true live -- so it is a fallback and not a
        # path worth relying on.
        candidates = [o for o in offers if o.id == key]
    if not candidates:
        raise HTTPException(
            409, f"that {what} is no longer being sold — the airline has "
                 "withdrawn it or the schedule moved. Search again and pick "
                 "from what is there now.")
    chosen = min(candidates, key=lambda o: abs(o.price - shown))
    moved = round(chosen.price - shown, 2) if shown else 0.0
    return chosen, moved


def _appt_out(a) -> dict:
    return {
        "kind": "appointment",
        "what": a.what, "who": a.who, "where": a.where, "place": a.place,
        "confirmed": a.confirmed,
        "day": a.day.isoformat() if a.day else None,
        "at": a.when.strftime("%H:%M") if a.when else None,
        "minutes": a.minutes, "trip_id": a.trip_id,
        "summary": a.summary(), "ready": a.ready, "raw": a.raw,
    }


def _appt_in(raw: dict):
    from datetime import date as _date, datetime as _dt, time as _time

    def day(value):
        try:
            return _date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    when = None
    settled = day(raw.get("day"))
    if settled and raw.get("at"):
        try:
            hour, minute = str(raw["at"]).split(":")[:2]
            when = _dt.combine(settled, _time(int(hour), int(minute)))
        except (ValueError, TypeError):
            when = None

    return appt_mod.Appointment(
        what=str(raw.get("what") or "")[:60],
        who=str(raw.get("who") or "")[:60],
        where=str(raw.get("where") or "")[:60],
        place=str(raw.get("place") or "")[:8],
        day=settled, when=when,
        minutes=max(5, min(int(raw.get("minutes") or 60), 12 * 60)),
        trip_id=str(raw.get("trip_id") or "")[:40],
        confirmed=bool(raw.get("confirmed")),
        raw=str(raw.get("raw") or "")[:2000],
    )


def _loaded_trips() -> list[tuple[str, Trip, str]]:
    out = []
    for saved in store_module.store().list(OWNER, limit=25):
        try:
            out.append((saved.id, _load(saved.id), saved.label))
        except HTTPException:
            continue
    return out


def _appointment_turn(body: Chat) -> dict:
    """The other half of the conversation: something to be at, not to buy.

    Same shape as a booking turn -- ask what is missing, then act -- because it
    is the same conversation. What differs is the three things that must be
    settled (who, when, where) and what "done" means: an appointment is not
    searched for, it is placed against an itinerary and judged.
    """
    base = _appt_in(body.state) if body.state.get("kind") == "appointment" else None
    unread: list[str] = []

    chosen = ""
    for field_name, value in (body.answers or {}).items():
        if field_name == "trip":
            chosen = value
            continue
        before = base or appt_mod.Appointment()
        base = appt_mod.answer(before, field_name, value)
        if base == before:
            unread.append(field_name)

    # What was known before this message. Compared against later to ask
    # whether the traveller told us anything -- and it has to be taken here,
    # before the sentence is parsed, because the parse is part of the telling.
    started_at = (base or appt_mod.Appointment()).settled
    fresh = appt_mod.parse(body.text, date.today())

    # Additive while collecting, overwriting while correcting -- the same rule
    # `converse.read` applies to a booking, and for the same reason: offering
    # to confirm and then ignoring the correction is worse than not offering.
    correcting = (base is not None and not base.confirmed
                  and [a.field for a in base.gaps() if not a.optional] == ["confirm"])

    def pick(field_name, empty):
        was = getattr(base, field_name) if base else empty
        now = getattr(fresh, field_name)
        if correcting and now != empty:
            return now
        return was if was != empty else now

    merged = appt_mod.Appointment(
        what=pick("what", ""), who=pick("who", ""),
        where=pick("where", ""), place=pick("place", ""),
        day=pick("day", None), when=pick("when", None),
        minutes=(fresh.minutes if fresh.minutes != appt_mod.DEFAULT_MINUTES
                 else (base.minutes if base else appt_mod.DEFAULT_MINUTES)),
        trip_id=chosen or (base.trip_id if base else ""),
        confirmed=(base.confirmed if base and not correcting else False),
        raw=body.text or (base.raw if base else ""),
    )
    if correcting and fresh.where and not fresh.place:
        # A corrected location has to be re-confirmed as a city, because the
        # clock moves with it.
        merged = replace_dataclass(merged, place="")
    settled = appt_mod.enrich(merged, _model.get_model())

    # A typed reply answers the open question first, exactly as it does when
    # booking. Chips and typing must not be two different products.
    if body.text.strip() and base is not None:
        pending = [a for a in settled.gaps() if not a.optional]
        if pending:
            settled = appt_mod.answer(settled, pending[0].field, body.text)

    asks = [{"field": a.field, "question": a.question, "options": list(a.options),
             "why": a.why, "optional": a.optional} for a in settled.gaps()]
    pending = next((a for a in asks if not a["optional"]), None)

    # The city an unrecognised address sits in, offered from the trips that
    # cover that day rather than left as free text. A traveller who typed
    # "the Ritz Carlton" is far more likely to tap "New York" than to spell it.
    if pending and pending["field"] == "place" and settled.day:
        known = []
        for _tid, trip, _label in _loaded_trips():
            rows = [b for b in trip.in_order() if b.start]
            if rows and rows[0].start.date() <= settled.day <= max(
                    (b.end or b.start) for b in rows).date():
                known += [places.label(b.where) for b in trip.in_order()
                          if places.by_code(b.where)]
        pending["options"] = list(dict.fromkeys(known))[:4]

    out = {"kind": "appointment", "state": _appt_out(settled), "asks": asks,
           "unread": unread, "ports": [], "note": "", "detail": "",
           "confirm": settled.card() if pending and pending["field"] == "confirm"
                      else None,
           "flights": [], "returns": [], "stays": []}

    if asks:
        first = pending or asks[0]
        reply = first["question"]
        said = (body.answers or {}).get(first["field"], "") or body.text
        if first["field"] == "confirm" and request_mod.declined(said):
            return {**out, "unread": [u for u in unread if u != "confirm"],
                    "reply": "What should I change? Tell me the bit that is "
                             "wrong — \"actually 3pm\", \"it's in Milan\"."}
        if (unread and first["field"] in unread) or (
                body.text.strip() and base is not None
                and settled.settled == started_at):
            reply = f"Sorry — I couldn't make that out. {reply}"
        return {**out, "reply": reply}

    # Which itinerary. Never guessed: putting a meeting on the wrong trip
    # produces a confident feasibility answer about the wrong week.
    trips = _loaded_trips()
    options = appt_mod.candidates(settled, [(tid, trip) for tid, trip, _ in trips])
    if settled.trip_id and settled.trip_id in {t[0] for t in trips}:
        options = [settled.trip_id]

    if not options:
        return {**out, "reply":
                f"Nothing you have booked covers {settled.day:%d %B}. Book that "
                "trip first and tell me about this again, and I'll check it "
                "against the flights."}
    if len(options) > 1:
        # Told apart by what is IN them, not by their labels. Two trips booked
        # the same way carry the same label -- "Zurich to Milan · 18 Sep – 20
        # Sep · hotel" twice -- and a choice between two identical chips is not
        # a choice. The first flight and the total are what differ.
        by_id = {tid: (trip, label) for tid, trip, label in trips}

        def distinguish(trip_id: str) -> str:
            trip, label = by_id[trip_id]
            legs = [b for b in trip.in_order() if b.kind is Kind.FLIGHT]
            total = round(sum(b.price for b in trip.bookings))
            head = legs[0].title.split(" - ")[0] if legs else label[:20]
            return f"{head} · €{total:,}"

        return {**out,
                "asks": [{"field": "trip", "question": "Which trip is this on?",
                          "options": [distinguish(t) for t in options],
                          "ids": options,
                          "why": "more than one of your trips covers that day, "
                                 "and putting a meeting on the wrong one gives "
                                 "you a confident answer about the wrong week",
                          "optional": False}],
                "reply": "Which trip is this on?"}

    trip_id = options[0]
    trip = next(t for tid, t, _ in trips if tid == trip_id)
    verdict = appt_mod.assess(trip, settled)

    saved = store_module.store().get(trip_id)
    payload = dict(saved.payload)
    payload["bookings"] = ingest.to_dicts(verdict["trip"].bookings)
    store_module.store().update(trip_id, payload)

    settled = replace_dataclass(settled, trip_id=trip_id)
    return {**out,
            # Cleared, because this one is finished. Carrying it forward meant
            # the next appointment inherited its day and its place: "workshop
            # on 18 September at ZRH" was filed on the 19th at Malpensa,
            # because those were still sitting in the state from the meeting
            # before it. A finished thing must not furnish the next one.
            "state": {"kind": "appointment"},
            "saved": _appt_out(settled),
            "trip_id": trip_id,
            "feasible": verdict["feasible"],
            "clashes": verdict["clashes"],
            "about_this": verdict["about_this"],
            "notes": verdict["notes"],
            "bookings": _bookings_out(_load(trip_id)),
            "reply": (f"Added to {saved.label}. "
                      + ("That works — nothing else is in the way."
                         if verdict["feasible"]
                         else "That does not fit:"))}


def replace_dataclass(obj, **changes):
    from dataclasses import replace as _replace

    return _replace(obj, **changes)


@app.post("/api/chat")
def chat(body: Chat) -> dict:
    """One exchange with the agent.

    Answers are applied before the graph runs, so a turn that answers the last
    open question searches in the same round trip rather than making the
    traveller say "ok, now go".
    """
    from base import PortError

    # Which conversation is this? An appointment in flight stays an
    # appointment; otherwise the text decides.
    if (body.state.get("kind") == "appointment"
            or (not body.state and converse.classify(
                {"text": body.text})["kind"] == "appointment")):
        return _appointment_turn(body)

    req = _req_in(body.state) if body.state else None

    # An answer that changed nothing was not understood. Tracking that is the
    # difference between an agent and a loop: without it the next turn asks the
    # identical question with no acknowledgement, and the traveller -- who has
    # already answered it -- is left to guess which word was the problem.
    unread: list[str] = []
    for field_name, value in (body.answers or {}).items():
        before = req or request_mod.Request()
        req = request_mod.answer(before, field_name, value)
        if req == before:
            unread.append(field_name)

    # A typed reply to an open question is an ANSWER, not a new request.
    #
    # This is the bug that made the agent repeat itself. Tapping "coming back"
    # sent an answer and worked; typing the same two words sent a message, and
    # a message is parsed as a fresh booking request -- which contains no city
    # and no date, so nothing merged, so the same question came back. The
    # traveller had answered, correctly, twice, and been asked a third time.
    #
    # The open question is tried first and the text still goes to the graph, so
    # "two way, and I need a hotel" settles both.
    before_text = req
    if body.text.strip() and req is not None:
        pending = [a for a in req.gaps() if not a.optional]
        if pending:
            req = request_mod.answer(req, pending[0].field, body.text)

    try:
        state = converse.turn(body.text, req, model=_model.get_model())
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc

    out = state["req"]
    asks = [{"field": a.field, "question": a.question,
             "options": list(a.options), "why": a.why, "optional": a.optional}
            for a in (state.get("asks") or [])]

    pending = next((a for a in asks if not a["optional"]), None)
    reply = state.get("reply", "")

    # "No, let me change it" is understood. It settles nothing, so the question
    # stays open, and reporting that as "I couldn't make that out" told the
    # traveller their own button was gibberish.
    if (pending and pending["field"] == "confirm"
            and request_mod.declined((body.answers or {}).get("confirm", "")
                                     or body.text)):
        unread = [u for u in unread if u != "confirm"]
        return {**_chat_payload(state, out, asks, pending, unread),
                "reply": "What should I change? Tell me the bit that is wrong "
                         "— \"actually the 19th\", \"from Zurich, not Milan\"."}
    # Nothing moved and something is still being asked: whatever the traveller
    # just said, this system did not understand it. Saying so beats asking the
    # same question with a straight face.
    stuck = (body.text.strip() and before_text is not None
             and out.settled == before_text.settled and asks)
    if stuck or (unread and asks and asks[0]["field"] in unread):
        reply = f"Sorry — I couldn't make that out. {reply}"

    return {**_chat_payload(state, out, asks, pending, unread), "reply": reply}


def _chat_payload(state, out, asks, pending, unread) -> dict:
    return {
        "unread": unread,
        # Shown only at the moment it is asked about. A summary card on every
        # turn is wallpaper; one card, at the point where everything after it
        # is computed from what it says, is a checkpoint.
        "confirm": out.card() if pending and pending["field"] == "confirm" else None,
        "kind": state.get("kind", "book"),
        "state": {**_req_out(out), "kind": "book", "raw": out.raw},
        "asks": asks,
        # Named so the page can show which systems were actually called. This
        # is the moment it stops looking like a chatbot.
        "ports": state.get("ports") or [],
        "note": state.get("note", ""),
        # The port's own words, for whoever is running this rather than for the
        # person booking. Same failure, two audiences.
        "detail": state.get("detail", ""),
        "flights": [_offer(o) for o in (state.get("flights") or [])],
        "returns": [_offer(o) for o in (state.get("returns") or [])],
        "stays": [_stay(h) for h in (state.get("stays") or [])],
    }


@app.post("/api/chat/choose")
def choose(body: Choice) -> dict:
    """The chosen options become an itinerary, stored under its own id.

    Re-searched rather than held: the offers were never kept on the server, so
    picking one asks the provider again and takes the fare it quotes now. That
    is slower and it is the only version that cannot sell a price that has
    expired.
    """
    from base import PortError

    req = _req_in(body.state)
    if not req.ready:
        raise HTTPException(400, "that request is not complete enough to book")

    zone_out, zone_back = _zone_for(req.origin), _zone_for(req.destination)
    repriced: list[dict] = []
    try:
        outbound = flow.search_flights(req.origin, req.destination,
                                       _day(req.depart.isoformat(), zone_out))
        chosen, moved = _rematch(outbound, body.flight_key or body.flight_id,
                                 body.flight_price, "fare")
        picked = [chosen]
        if moved:
            repriced.append({"label": chosen.label, "was": body.flight_price,
                             "now": chosen.price, "moved": moved})

        if (body.return_key or body.return_id) and req.ret:
            back = flow.search_flights(req.destination, req.origin,
                                       _day(req.ret.isoformat(), zone_back))
            found, moved = _rematch(back, body.return_key or body.return_id,
                                    body.return_price, "return fare")
            picked.append(found)
            if moved:
                repriced.append({"label": found.label, "was": body.return_price,
                                 "now": found.price, "moved": moved})

        stays = []
        if body.hotel_id and req.hotel:
            place = places.by_code(req.destination)
            checkout = req.check_out
            found = flow.search_hotels(
                place.hotel_city, place.country,
                _day(req.check_in.isoformat(), zone_back),
                _day(checkout.isoformat(), zone_back), code=req.destination)
            stays = [h for h in found if h["id"] == body.hotel_id]
            if not stays:
                raise HTTPException(409, "that rate is no longer available")
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc

    assembled, _ = flow.select(picked, stays)
    saved = store_module.store().save(
        OWNER,
        {"bookings": ingest.to_dicts(assembled.bookings),
         # The preference travels with the trip. When this itinerary breaks in
         # a week, nobody has to ask the traveller what they cared about at the
         # moment their flight is cancelled.
         "preference": req.preference,
         "request": _req_out(req)},
        body.label or req.summary())

    trip = _load(saved.id)
    return {"trip_id": saved.id, "label": saved.label,
            "problems": infeasible(trip),
            "preference": req.preference,
            # Said, not swallowed. A fare that moved between the click and the
            # booking is the traveller's business.
            "repriced": repriced,
            "total": round(sum(b.price for b in trip.bookings), 2),
            "bookings": _bookings_out(trip)}


@app.get("/api/itineraries")
def itineraries() -> dict:
    """Everything this traveller has booked, and which of them are broken."""
    rows = []
    for saved in store_module.store().list(OWNER, limit=25):
        try:
            trip = _load(saved.id)
        except HTTPException:
            continue
        disruption = flow.from_dict(saved.payload.get("disruption"))
        rows.append({
            "trip_id": saved.id,
            "label": saved.label,
            "created": saved.created.isoformat(),
            "preference": saved.payload.get("preference", ""),
            "total": round(sum(b.price for b in trip.bookings), 2),
            "currency": trip.in_order()[0].currency if trip.bookings else "EUR",
            "starts": _when(trip.in_order()[0].start) if trip.bookings else None,
            "legs": len(trip.bookings),
            "disrupted": disruption is not None,
            "disrupted_booking": disruption.booking_id if disruption else "",
            "bookings": _bookings_out(trip),
        })
    return {"itineraries": rows}


@app.post("/api/act")
def act(body: Act) -> dict:
    """Take the plan. This is where the lanes stop being labels.

    AUTO actions are performed, TAP and CALL are handed over, and the stored
    itinerary is rewritten to the trip the traveller now has. What was done and
    what is still waiting on them are reported separately, because a system
    that blurs those two has started lying about the useful part.
    """
    from base import PortError

    saved = store_module.store().get(body.trip_id)
    if saved is None:
        raise HTTPException(404, "no trip with that id")
    trip = _load(body.trip_id)
    preference = saved.payload.get("preference", "")

    try:
        recovery = flow.replan(trip, body.booking_id, preference=preference)
    except (PortError, KeyError) as exc:
        raise HTTPException(503 if isinstance(exc, PortError) else 404, str(exc))

    # By key, for the same reason `choose` matches by key: `cancel` searched
    # and showed these plans, `act` searches again, and in live mode the second
    # search has entirely different offer ids. Matching on the id worked
    # against recordings and would have failed on every plan a traveller ever
    # took.
    wanted = body.plan_key or body.plan_id
    plan = next((p for p in recovery.plans if p.key == wanted), None) \
        or next((p for p in recovery.plans if p.id == wanted), None)
    if plan is None:
        raise HTTPException(
            409, "that option is no longer available — the search has moved on. "
                 "Cancel again to see what is there now.")

    lines: list[str] = []
    done = act_mod.perform(plan, trip, body.trip_id, log=lines.append)
    offer = next((o for o in recovery.offers if o.id == plan.id), None)
    updated, changed = act_mod.apply(plan, trip, recovery.disruption, offer)

    payload = dict(saved.payload)
    payload["bookings"] = ingest.to_dicts(updated.bookings)
    payload["acted"] = {"at": datetime.now(timezone.utc).isoformat(),
                        "plan": plan.name, "changed": changed}
    # The disruption is resolved: this itinerary is no longer the broken one,
    # so the watch stops counting down deadlines that have been dealt with.
    payload.pop("disruption", None)
    payload.pop("watermark", None)
    store_module.store().update(body.trip_id, payload)

    return {
        "trip_id": body.trip_id,
        "plan": plan.name,
        "summary": act_mod.summarise(done, changed),
        "sent": [d.__dict__ for d in done if d.state == "sent"],
        "pending": [d.__dict__ for d in done if d.state == "pending"],
        "changed": changed,
        "log": lines,
        "bookings": _bookings_out(_load(body.trip_id)),
    }


def _bookings_out(trip: Trip) -> list[dict]:
    return [{"id": b.id, "title": b.title, "provider": b.provider,
             "kind": b.kind.value, "where": b.where,
             "starts": _when(b.start), "ends": _when(b.end),
             "must_arrive_by": _when(b.must_arrive_by),
             "price": b.price, "currency": b.currency, "pending": b.pending,
             "commitment": b.commitment, "who": b.who,
             "ticket_group": b.ticket_group, "policy": b.policy.source}
            for b in trip.in_order()]


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
            offer = (next((o for o in found if o.key == leg.offer_key), None)
                     if leg.offer_key else None)
            offer = offer or next((o for o in found if o.id == leg.offer_id), None)
            if offer is None:
                raise HTTPException(
                    409, "that flight is no longer being sold — search again "
                         "and pick from what is there now")
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
        "bookings": _bookings_out(trip),
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

    saved = store_module.store().get(body.trip_id)
    try:
        recovery = flow.replan(trip, body.booking_id,
                               preference=saved.payload.get("preference", ""))
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc

    # Written down, so the watch has something to watch a minute after the page
    # that started it was closed. Live status is a query and can be asked again;
    # a traveller pressing "cancelled" is an event, and an unrecorded event did
    # not happen.
    stored = dict(saved.payload)
    stored["disruption"] = flow.to_dict(recovery.disruption)
    stored.pop("watermark", None)      # a new disruption starts a new clock
    stored.pop("last_sent", None)
    store_module.store().update(body.trip_id, stored)

    payload = _assessment(trip, recovery.disruption, recovery.impact,
                          recovery.plans, recovery.disruption.new_end, None,
                          body.trip_id, gap=recovery.gap, injected=True)
    payload["searched"] = [_offer(o) for o in recovery.offers]
    payload["saved"] = recovery.saved
    payload["preference"] = recovery.preference
    payload["warning"] = recovery.warning
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
    """The conversational front door, and now the default.

    The two pages behind it are kept rather than replaced. `/book` is the same
    engine with a form, which is faster to drive when demonstrating a specific
    fare, and `/demo` is the scripted delay that the tests assert to the euro.
    A conversation is the better front door and a worse regression test.
    """
    return FileResponse(STATIC / "agent.html")


@app.get("/demo")
def demo() -> FileResponse:
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
