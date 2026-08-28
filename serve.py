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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from fastapi import FastAPI, HTTPException                    # noqa: E402
from fastapi.responses import FileResponse                    # noqa: E402
from pydantic import BaseModel                                # noqa: E402

import _model                                                 # noqa: E402
import ingest                                                 # noqa: E402
import store as store_module                                  # noqa: E402
from domain import Kind, Trip                                 # noqa: E402
import aerodatabox, duffel, rail                              # noqa: E402,E401
from base import MODE, degraded, shifted                      # noqa: E402
from demo_trip import DEFAULT_BASE, anchor, build_trip, resolve_base   # noqa: E402
from graph import propagate                                   # noqa: E402
from plan import Lane, generate                               # noqa: E402

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
        # The one number in the engine no reachable API can quote. Read off the
        # field, never off the prose.
        "estimated": a.price_source == "estimate",
    }


def _plan(p, best: bool) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "tagline": p.tagline,
        "net_cash": p.net_cash,
        "total_damage": p.total_damage,
        "wasted": p.wasted,
        "arrives_at": _when(p.arrives_at),
        "arrives_where": p.arrives_where,
        "best": best,
        "estimated": any(a.price_source == "estimate" for a in p.actions),
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
            "delay_minutes": int(disruption.delay(trip).total_seconds() // 60),
            "was": _when(source.end),
            "now_arrives": _when(disruption.new_end),
            "confidence": disruption.confidence,
            "reason": disruption.reason,
        },
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


@app.get("/api/state")
def state(base: str = "today", trip: str = "") -> dict:
    """The whole assessment. Same two engine calls the CLI makes.

    Without ``trip`` this answers about the built-in scenario, which is what the
    demo and the tests use. With one, it answers about a real stored itinerary.
    """
    from base import PortError

    loaded = None
    if trip:
        saved = store_module.store().get(trip)
        if saved is None:
            raise HTTPException(404, "no trip with that id")
        bookings, problems, _sources = ingest.to_bookings(saved.payload)
        if not bookings:
            raise HTTPException(422, {"problems": problems})
        loaded = Trip(bookings)

    try:
        return _assemble(_base(base), loaded, trip)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


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
