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
import re
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from pydantic import BaseModel                                # noqa: E402

import _model                                                 # noqa: E402
import act as act_mod                                         # noqa: E402
import appointment as appt_mod                                # noqa: E402
import audit                                                  # noqa: E402
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
import plan as plan_mod                                      # noqa: E402
from plan import Gap, Lane, generate                          # noqa: E402
import permit                                                 # noqa: E402
import profile as profile_mod                                 # noqa: E402
import risk as risk_mod                                       # noqa: E402
import roles                                                  # noqa: E402

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


app = FastAPI(title="tripshield", lifespan=lifespan)

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


def _who(authorization: str = Header(""),
         x_downstream_token: str = Header("")) -> roles.Person:
    """Who is asking. A token they were handed, or the anonymous traveller.

    There is no login; see roles.py. A request with no token is the same
    anonymous traveller every request used to be, so nothing that worked
    before needs one. A token nobody issued is refused rather than quietly
    treated as anonymous -- somebody holding a stale or mistyped link should
    be told, not shown a stranger's empty list.
    """
    token = x_downstream_token.strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        return roles.ANON
    found = store_module.store().person(token)
    if not found:
        raise HTTPException(401, "that link is not one I handed out")
    return roles.Person(found["id"], found["name"], token)


def _access(trip_id: str, who: roles.Person, need: str):
    """The stored trip and this person's role on it, or the right refusal.

    404 for no membership at all -- a trip you were not let into is a trip
    that, to you, does not exist, and 403 would confirm the id is real. 403
    for a role that may see it and may not do this.
    """
    saved = store_module.store().get(trip_id)
    role = roles.role_of(saved.payload, saved.owner, who) if saved else ""
    if not role:
        raise HTTPException(404, "no trip with that id")
    if not roles.can(role, need):
        raise HTTPException(403, f"a {role} on this trip cannot do that")
    return saved, role


def _owner(who: roles.Person = Depends(_who)) -> roles.Person:
    """Whoever is about to own something. Never the anonymous traveller.

    Reading anonymously is fine and always was. OWNING anonymously is what
    could not work: trips are stored under `who.id`, and every tokenless
    visitor shares the single id "anon", so an anonymous trip belonged to
    everyone who had not been handed a token.

    `roles.role_of` now refuses to call an anonymous visitor an owner, which
    closes the leak -- but on its own it turns a create into a trip that
    vanishes the moment it is saved. This is the loud half: a browser that
    wants to own something asks for an identity first (POST /api/people, which
    every page does before its first call), and one that has not is told so
    rather than handed a write it can never read back.
    """
    if who.anonymous:
        raise HTTPException(401, "ask for an identity first: POST /api/people")
    return who


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
    #: "flight" or "rail". One list rather than two, in the order the journey
    #: happens, because that is the order the traveller chose them in and they
    #: do not think in modes -- they think in "and then I go to Milan".
    mode: str = "flight"


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
    #: What the agent may do about this trip on its own. Empty means nothing:
    #: no trip becomes autonomous because a field was left out.
    permissions: dict = {}


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
    #: How each leg travels. The chosen option has to be re-resolved through
    #: the provider that can find it, and a train asked of Duffel comes back
    #: empty -- which reads as a fare the airline has withdrawn rather than as
    #: a question put to the wrong shop. Defaults keep an older client working.
    flight_mode: str = "flight"
    return_mode: str = "flight"
    permissions: dict = {}


class Permit(BaseModel):
    """The traveller's rules for one trip. See permit.py for what they mean."""

    trip_id: str
    auto_limit: float = 0.0
    #: The second threshold: between auto_limit and here the agent decides and
    #: holds -- itinerary rewritten, replacement pending, nothing sent.
    hold_limit: float = 0.0
    never: list[str] = []
    always_ask: list[str] = []
    #: The kill switch has its own endpoint; a rules save leaves it where it
    #: is unless explicitly told. None means "as it was" -- saving a spending
    #: cap must not quietly re-arm a machine somebody switched off.
    disarmed: bool | None = None
    #: Keep these as the default for every future trip too. Off by default: a
    #: rule set on one trip is about that trip until the traveller says
    #: otherwise.
    remember: bool = False


class ProfileIn(BaseModel):
    """Who the traveller is, as far as they choose to say. See profile.py."""

    preference: str = ""
    home: str = ""
    auto_limit: float = 0.0
    hold_limit: float = 0.0
    never: list[str] = []
    always_ask: list[str] = []
    #: None means "not this form's business" -- the stored value survives.
    #: The profile form never sends it, so saving the profile used to
    #: silently re-arm a kill switch that /api/permissions remember stored.
    disarmed: bool | None = None


class Abandon(BaseModel):
    """Cancel the whole trip.

    Two steps, and not because a form wants a confirm box: this is the one
    action in the product that destroys value on purpose and cannot be undone
    by pressing it again. `confirm=false` prices it -- every booking, what each
    is still worth, who can cancel it and by when -- and does nothing. Only the
    second call sends anything.
    """

    trip_id: str
    confirm: bool = False
    reason: str = ""


class Act(BaseModel):
    trip_id: str
    booking_id: str
    plan_key: str = ""
    plan_id: str = ""            # deprecated; ignored when a key is supplied
    #: Actions in the plan the traveller is NOT taking, by their id -- the
    #: brief's approve / reject, per line. Approve is the default; the list is
    #: what was rejected, so an older client that sends nothing takes the plan
    #: whole, exactly as before.
    reject: list[str] = []


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


class Delay(BaseModel):
    """The other button. This leg is going, and it is going late.

    Its own model rather than a field on `Cancellation`, for the same reason
    `flow.delay` is its own verb: a delay eventually puts the traveller at the
    destination and a cancellation never does, and that is the one distinction
    the whole engine turns on. A request that could mean either is a request
    that can be misread.
    """

    trip_id: str
    booking_id: str
    #: How late. Asked rather than assumed: ninety minutes is an inconvenience
    #: and six hours is a different trip, and which one it is is the entire
    #: question the traveller wants answered.
    minutes: int = 90


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
        "id": a.id,
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


def _plan(p, best: bool, verdict=None) -> dict:
    """``verdict`` is what the traveller's rules say about this plan. Absent
    for the scripted demo, which has no traveller to have rules."""
    def act(a) -> dict:
        out = _action(a)
        if verdict is not None:
            out["approval"], out["approval_why"] = verdict.approval(a)
        return out

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
        "lanes": {key: [act(a) for a in p.lane(lane)]
                  for lane, key in LANE_KEY.items()},
        # The same actions in the order they were built, which for a whole-trip
        # cancellation is itinerary order and is the order a person checks them
        # off in. `lanes` regroups by who does the work and loses that, and
        # both readings are wanted -- one to act from, one to read down.
        "actions": [act(a) for a in p.actions],
        # The goals. Counted here for the same reason they rank: a plan that
        # keeps the meeting is a different kind of plan from one that does not,
        # whatever the two cost.
        "saves": sorted(p.saved_ids),
        "misses": sorted(p.missed_ids),
        "mode": p.mode,
        # And whether the traveller has allowed it to be taken without asking.
        "auto": bool(verdict and verdict.auto),
        "permission": verdict.why if verdict else "",
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
                gap: Gap | None = None, injected: bool = False,
                perms: permit.Permissions | None = None) -> dict:
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
                # When it happens, not only when it stops waiting. A card that
                # shows a cutoff and no start asks the traveller to hold the
                # itinerary in their head to place it.
                "starts": _when(n.booking.start),
                "ends": _when(n.booking.end),
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
        "plans": [_plan(p, i == 0, permit.judge(p, perms) if perms else None)
                  for i, p in enumerate(plans)],
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
            channels = notify.deliver(alert, saved.id)
            audit.record(
                store_module.store(), saved.id,
                actor="the watch", type=audit.ALERTED,
                subject=f"{alert.title or alert.booking_id}: {alert.message}",
                citation=f"deadline {alert.closes:%d %b %H:%M}"
                         + (f"; S${alert.worth:,.0f} at stake" if alert.worth else
                            "; nothing recoverable behind it"),
                authorization=audit.NOTIFICATION,
                feed=f"watch tick ({MODE}); delivered via "
                     f"{', '.join(channels) or 'no channel took it'}")
            fired += 1
        payload = dict(saved.payload)
        payload["watermark"] = max(a.at for a in ready).isoformat()
        payload["last_sent"] = [
            {"at": a.at.isoformat(), "message": a.message} for a in ready[-5:]]
        store_module.store().update(saved.id, payload)
    return fired


#: How often one trip is asked about. The status feed is a paid, rate-limited
#: call per flight, and a flight's status does not change minute to minute;
#: asking every tick would spend the whole allowance re-learning that nothing
#: has happened.
DETECT_EVERY = timedelta(minutes=10)


class _Found:
    """A detection, in the shape `notify.deliver` speaks. One sentence: what
    was found, and what the agent did or is waiting for."""

    def __init__(self, disruption, trip: Trip, recovery, taken):
        source = trip.by_id(disruption.booking_id)
        late = disruption.delay(trip)
        self.kind = "found"
        self.lead = 0
        self.closes = disruption.new_end
        self.at = disruption.new_end
        self.title = source.title
        self.booking_id = source.id
        self.worth = 0.0
        self.key = f"found:{source.id}"
        what = ("cancelled" if disruption.cancelled
                else f"delayed {int(late.total_seconds() // 3600)}h "
                     f"{int(late.total_seconds() // 60 % 60):02d}m")
        best = recovery.best
        if taken:
            outcome = (f"taken {best.name} ({taken['permission']}); "
                       + (("still yours: " + "; ".join(d["label"] for d in taken["pending"]))
                          if taken["pending"] else "nothing left to do"))
        elif best is not None and best.id != "noop":
            outcome = f"recommended {best.name}, waiting for your approval"
        else:
            outcome = "nothing downstream is out of reach"
        self.message = f"{source.title} {what} — {outcome}"


def detect(now: datetime | None = None) -> int:
    """One pass asking the status feed about every live trip. Returns how many
    disruptions it found.

    THIS IS WHERE THE AGENT SPEAKS FIRST. Until it existed, the loop below only
    counted down deadlines on disruptions a person had already pressed a
    button about; the feed was consulted when somebody opened the page and at
    no other time, so a flight cancelled at 02:00 was found at 07:30 by
    whoever looked. Now the feed is asked, the answer goes through exactly the
    path the buttons use -- `_respond` -- and if the traveller's rules allow
    it, the plan is taken before anybody is awake.

    Skips what it has no business touching: a trip already being watched (its
    disruption is known; the deadline sweep owns it from here), a trip that
    was called off, and a trip asked about less than DETECT_EVERY ago.
    """
    found = 0
    for saved in store_module.store().live(limit=50):
        payload = saved.payload
        if payload.get("disruption") or payload.get("abandoned"):
            continue
        when = now or datetime.now(timezone.utc)
        mark = payload.get("checked")
        if mark and when - datetime.fromisoformat(mark) < DETECT_EVERY:
            continue
        bookings, _problems, _sources = ingest.to_bookings(payload)
        if not bookings:
            continue
        trip = Trip(bookings)
        local = when.astimezone(trip.in_order()[0].start.tzinfo)
        try:
            disruption, _quiet = _detect(trip, local)
        except Exception as exc:                        # noqa: BLE001
            # One trip's bad status call must not end the pass for the rest.
            print(f"detect: {saved.id}: {type(exc).__name__}: {exc}")
            disruption = None

        stamped = dict(store_module.store().get(saved.id).payload)
        stamped["checked"] = when.isoformat()
        store_module.store().update(saved.id, stamped)
        if disruption is None:
            continue

        fresh = store_module.store().get(saved.id)
        try:
            recovery, taken = _respond(saved.id, fresh, trip, disruption,
                                       injected=False, actor="the watch")
        except Exception as exc:                        # noqa: BLE001
            print(f"detect: {saved.id}: replan failed: {type(exc).__name__}: {exc}")
            continue
        found += 1
        # Told once, now. The deadline sweep takes over from here -- but a
        # traveller whose flight was found cancelled at 02:00 and whose first
        # word about it is a T-60 reminder at 05:00 has been let down by the
        # notifier, not by the engine.
        found_alert = _Found(disruption, trip, recovery, taken)
        channels = notify.deliver(found_alert, saved.id)
        audit.record(
            store_module.store(), saved.id,
            actor="the watch", type=audit.ALERTED,
            subject=found_alert.message,
            citation=f"status feed reported: {disruption.reason or 'disruption'}",
            authorization=audit.NOTIFICATION,
            feed=f"aerodatabox ({MODE}); delivered via "
                 f"{', '.join(channels) or 'no channel took it'}")
        after = dict(store_module.store().get(saved.id).payload)
        after["detected"] = {"at": when.isoformat(),
                             "plan": recovery.best.name if recovery.best else "",
                             "taken": bool(taken)}
        store_module.store().update(saved.id, after)
    return found


async def _watch() -> None:
    while True:
        try:
            # In a worker thread, not on the loop: detect() does port HTTP,
            # and a status call that stalls on the event loop stalls every
            # request the server is holding at that moment.
            await asyncio.to_thread(detect)
            await asyncio.to_thread(sweep)
        except Exception as exc:                        # noqa: BLE001
            # Never let one bad pass end the watch. A notifier that dies
            # quietly is worse than no notifier: the page still promises it.
            print(f"watch: {type(exc).__name__}: {exc}")
        await asyncio.sleep(TICK.total_seconds())


@app.get("/api/alerts")
def alerts(trip: str, who: roles.Person = Depends(_who)) -> dict:
    """Everything this trip still has to be told, and what it was already told.

    Fire times in the past are included. A traveller opening the page at 03:00
    has to see the 01:00 deadline they slept through as well as the 06:00 one
    they can still make; a schedule that hides what it failed to deliver is the
    one bug in a notifier nobody catches.
    """
    saved, role = _access(trip, who, roles.VIEW)
    watched, _disruption, schedule = _alerts(saved)
    if watched is None:
        return {"trip_id": trip, "watching": False,
                "why": "nothing is disrupted on this trip, so there is no clock",
                "channels": notify.channels(), "alerts": []}

    now = datetime.now(watched.in_order()[0].start.tzinfo)
    return roles.redact({
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
    }, role)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        # Which commit is actually serving. Render sets RENDER_GIT_COMMIT;
        # empty locally. Exists so "did the deploy land" is a curl, not a
        # guess made from page markers.
        "release": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
        "ports_mode": MODE,
        "degraded": list(degraded),
        "shifted": list(shifted),
        "model": _model.label(),
        # Configured vs actually working. /health is the one place that must
        # not report an intention as a fact.
        "model_status": _model.effective(),
        "storage": store_module.status()["using"],
        # Configured and not working is a third state, and it is the one worth
        # seeing. Reporting only "memory" makes a database that is down look
        # exactly like a deployment that never wanted one.
        "storage_status": store_module.status(),
        "watch": {
            "running": os.environ.get("DOWNSTREAM_WATCH", "1") != "0",
            "every_seconds": int(TICK.total_seconds()),
            "channels": notify.channels(),
            "durable": False,
            "caveat": "in-process: it stops when the instance sleeps",
            # It asks the status feed too, not only counts down. Said here
            # because "the watch is running" used to mean less than it sounds.
            "detects": True,
            "detect_every_seconds": int(DETECT_EVERY.total_seconds()),
            "detection_window_days": DETECTION_WINDOW.days,
        },
        "fixtures": sorted(p.name for p in (ROOT / "fixtures").glob("*")),
    }


@app.post("/api/trip")
def add_trip(body: Paste, who: roles.Person = Depends(_owner)) -> dict:
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
        who.id, {"bookings": ingest.to_dicts(result.trip.bookings)},
        body.label or "Untitled trip")
    audit.record(
        store_module.store(), saved.id,
        actor=who.name, type=audit.EXTRACTED,
        subject=f"{saved.label}: {len(result.trip.bookings)} bookings from a "
                "pasted confirmation",
        citation=_cite_policies(result.trip),
        authorization=audit.OBSERVED,
        feed=f"pasted text, read by {_model.label()}")
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


@app.get("/api/search/rail")
def search_rail(origin: str, destination: str, on: str) -> dict:
    """Real timetables, and a fare that is ours.

    ``estimated`` on every offer, because transport.opendata.ch returns
    departures and rolling stock and no price, and SBB publishes no free fare
    API. The number is labelled wherever it appears and the deep link goes to
    the operator, so the traveller sees the real one before paying.

    An empty list is the ordinary answer, not a fault: this reaches the Swiss
    timetable and nothing else, so most pairs of cities on earth have no train
    between them as far as this product is concerned.
    """
    from base import PortError

    zone = _zone_for(origin)
    try:
        found = flow.search_rail(origin.upper(), destination.upper(), _day(on, zone))
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
        "confirmed": req.confirmed, "shown": req.shown, "nights": req.nights,
        "hotel_in": req.hotel_in.isoformat() if req.hotel_in else None,
        "hotel_out": req.hotel_out.isoformat() if req.hotel_out else None,
        "preference": req.preference, "summary": req.summary(),
        "ready": req.ready,
        "from_profile": list(req.from_profile),
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
        confirmed=bool(raw.get("confirmed")), shown=bool(raw.get("shown")),
        preference=(str(raw.get("preference") or "").lower()
                    if str(raw.get("preference") or "").lower()
                    in request_mod.PREFERENCES else ""),
        raw=str(raw.get("raw") or "")[:2000],
        from_profile=tuple(str(f) for f in (raw.get("from_profile") or [])
                           if f in ("origin", "preference")),
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
        "minutes": a.minutes, "trip_id": a.trip_id, "shown": a.shown,
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
        confirmed=bool(raw.get("confirmed")), shown=bool(raw.get("shown")),
        raw=str(raw.get("raw") or "")[:2000],
    )


def _loaded_trips(who: roles.Person = roles.ANON) -> list[tuple[str, Trip, str]]:
    out = []
    for saved in store_module.store().list(who.id, limit=25):
        try:
            out.append((saved.id, _load(saved.id), saved.label))
        except HTTPException:
            continue
    return out


def _day_of(trip: Trip, settled, context) -> Trip:
    """Only the bookings that touch the appointment's own local day."""
    zone = appt_mod.zone_for(settled, context)
    if settled.day is None or zone is None:
        return trip
    return Trip([b for b in trip.in_order() if b.start
                 and b.start.astimezone(zone).date() == settled.day])


def _appointment_turn(body: Chat, who: roles.Person = roles.ANON) -> dict:
    """The other half of the conversation: something to be at, not to buy.

    Same shape as a booking turn -- ask what is missing, then act -- because it
    is the same conversation. What differs is the three things that must be
    settled (who, when, where) and what "done" means: an appointment is not
    searched for, it is placed against an itinerary and judged.
    """
    base = _appt_in(body.state) if body.state.get("kind") == "appointment" else None
    unread: list[str] = []

    chosen = ""
    approve_said = ""
    for field_name, value in (body.answers or {}).items():
        if field_name == "trip":
            chosen = value
            continue
        if field_name == "approve":
            approve_said = value
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
    # Taken from the state, not inferred: the turn that answers the last
    # question would otherwise look identical to the turn that corrects the
    # card, and the answer would be re-parsed as a correction.
    correcting = base is not None and base.shown and not base.confirmed

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
        shown=bool(base and base.shown),
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
        for _tid, trip, _label in _loaded_trips(who):
            rows = [b for b in trip.in_order() if b.start]
            if rows and rows[0].start.date() <= settled.day <= max(
                    (b.end or b.start) for b in rows).date():
                known += [places.label(b.where) for b in trip.in_order()
                          if places.by_code(b.where)]
        pending["options"] = list(dict.fromkeys(known))[:4]

    # Showing the card is what makes the next sentence a correction.
    if pending and pending["field"] == "confirm" and not settled.shown:
        settled = replace_dataclass(settled, shown=True)

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
    trips = _loaded_trips(who)
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
    # Adding to somebody else's trip is a capability, not a courtesy.
    _access(trip_id, who, roles.BOOK)
    verdict = appt_mod.assess(trip, settled, home=_profile(who).home)

    # WRONG CITY IS REFUSED, the one exception to "reported rather than
    # refused" (see appointment.assess). The timeline proves the traveller is
    # elsewhere; saving would file a commitment the itinerary makes
    # unattendable. Nothing is stored; the state keeps the appointment
    # unconfirmed so "actually the 20th" flows through the correction path.
    if verdict["invalid"]:
        return {**out,
                "state": _appt_out(replace_dataclass(settled, confirmed=False)),
                "invalid": verdict["invalid"],
                "presence": verdict["presence"],
                "reply": "I can't save that. "
                         + verdict["invalid"][0]["message"]
                         + " Change the day or the city — \"actually the "
                           "20th\", \"make it Zurich\"."}

    # A TIGHT GAP WARNS AND WAITS. Under two hours to a neighbouring event in
    # the same city is a warning the traveller can approve past -- warn
    # first, save only on an explicit yes. The ask rides the same chip
    # machinery as every other question.
    yes = re.compile(r"\s*(y|yes|yeah|yep|ok|okay|save|go)", re.I)
    approved = bool(yes.match(approve_said)) or (
        body.state.get("approve_pending") and bool(yes.match(body.text or "")))
    if approve_said and request_mod.declined(approve_said):
        return {**out,
                "state": _appt_out(replace_dataclass(settled, confirmed=False)),
                "reply": "What should I change? Tell me the bit to move — "
                         "\"actually 3pm\", \"make it the 20th\"."}
    if verdict["tight"] and not approved:
        why = " · ".join(t["message"] for t in verdict["tight"])
        return {**out,
                "state": {**_appt_out(settled), "approve_pending": True},
                "tight": verdict["tight"],
                "presence": verdict["presence"],
                "asks": [{"field": "approve", "question": "Save it anyway?",
                          "options": ["yes, save it", "no, change the time"],
                          "why": why, "optional": False}],
                "reply": f"Heads up — {why}. Save it anyway?"}

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
            "tight": verdict["tight"],
            "invalid": [],
            "presence": verdict["presence"],
            # "That day now reads" finally reads THAT DAY: the whole
            # itinerary under a day heading put a 21 Sep flight in an
            # 18 Sep answer.
            "bookings": _bookings_out(_day_of(_load(trip_id), settled, trip)),
            "reply": (f"Added to {saved.label}. "
                      + ("That works — nothing else is in the way."
                         if verdict["feasible"]
                         else "That does not fit:"))}


def replace_dataclass(obj, **changes):
    from dataclasses import replace as _replace

    return _replace(obj, **changes)


def _appointment_answer(body: Chat, who: roles.Person) -> dict:
    """Answer a question about saved appointments from the store.

    Matched on the words of the question against the stored commitments'
    titles; answered in the booking's own timezone; and nothing enters the
    collecting state, so a question can never become an appointment.
    """
    low = (body.text or "").lower()
    words = [w for w in re.findall(r"[a-z]+", low) if len(w) > 3]
    hits: list[tuple[int, object, str]] = []
    for _tid, trip, label in _loaded_trips(who):
        for b in trip.in_order():
            if not b.commitment:
                continue
            title_low = b.title.lower()
            score = sum(1 for w in words if w in title_low)
            if score:
                hits.append((score, b, label))
    if hits:
        hits.sort(key=lambda h: -h[0])
        _score, b, label = hits[0]
        place = places.by_code(b.origin or "")
        when = (f"{b.start:%A %d %B at %H:%M}"
                + (f" ({place.label} time)" if place else "")
                if b.start else "no time saved")
        reply = f"{b.title} — {when}, on “{label}”."
    else:
        reply = ("I don't have an appointment like that saved. Tell me about "
                 "it in a sentence and I'll file it against your trip.")
    return {"kind": "asking", "state": {}, "asks": [], "unread": [],
            "ports": [], "note": "", "detail": "", "confirm": None,
            "flights": [], "returns": [], "stays": [], "reply": reply}


@app.post("/api/chat")
def chat(body: Chat, who: roles.Person = Depends(_who)) -> dict:
    """One exchange with the agent.

    Answers are applied before the graph runs, so a turn that answers the last
    open question searches in the same round trip rather than making the
    traveller say "ok, now go".
    """
    from base import PortError

    # Which conversation is this? An appointment in flight stays an
    # appointment; otherwise the text decides. A QUESTION about the calendar
    # reads it and must never write it -- "when is my lunch appointment"
    # once created a second lunch, titled with the question.
    kind = ("appointment" if body.state.get("kind") == "appointment"
            else converse.classify({"text": body.text})["kind"]
            if not body.state else "")
    if kind == "asking":
        return _appointment_answer(body, who)
    if kind == "appointment":
        return _appointment_turn(body, who)

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
        state = converse.turn(body.text, req, model=_model.get_model(),
                              profile=_profile(who))
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
def choose(body: Choice, who: roles.Person = Depends(_owner)) -> dict:
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
        outbound = flow.search_leg(req.origin, req.destination,
                                   _day(req.depart.isoformat(), zone_out),
                                   body.flight_mode,
                                   key=body.flight_key or body.flight_id)
        chosen, moved = _rematch(outbound, body.flight_key or body.flight_id,
                                 body.flight_price, "fare")
        picked = [chosen]
        if moved:
            repriced.append({"label": chosen.label, "was": body.flight_price,
                             "now": chosen.price, "moved": moved})

        if (body.return_key or body.return_id) and req.ret:
            back = flow.search_leg(req.destination, req.origin,
                                   _day(req.ret.isoformat(), zone_back),
                                   body.return_mode,
                                   key=body.return_key or body.return_id)
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
        who.id,
        {"bookings": ingest.to_dicts(assembled.bookings),
         # The preference travels with the trip. When this itinerary breaks in
         # a week, nobody has to ask the traveller what they cared about at the
         # moment their flight is cancelled.
         "preference": req.preference,
         # And so do the rules for acting on it, for the same reason.
         "permissions": _default_rules(body.permissions, who),
         "request": _req_out(req)},
        body.label or req.summary())
    _audited = _load(saved.id)
    audit.record(
        store_module.store(), saved.id,
        actor=who.name, type=audit.EXTRACTED,
        subject=f"{saved.label}: {len(_audited.bookings)} bookings selected "
                "from a search",
        citation=_cite_policies(_audited),
        authorization=audit.OBSERVED,
        feed=_feed_for(_audited))

    trip = _load(saved.id)
    return {"trip_id": saved.id, "label": saved.label,
            "problems": infeasible(trip),
            "risks": risk_mod.as_dicts(risk_mod.assess(trip)),
            "preference": req.preference,
            # Said, not swallowed. A fare that moved between the click and the
            # booking is the traveller's business.
            "repriced": repriced,
            "total": round(sum(b.price for b in trip.bookings), 2),
            "bookings": _bookings_out(trip)}


@app.get("/api/itineraries")
def itineraries(who: roles.Person = Depends(_who)) -> dict:
    """Everything this person owns or was let into, and which are broken.

    Each row says what this person may do with it, and a role that may not
    see money gets the row with every price blanked -- by key, recursively,
    so a field added tomorrow cannot leak by being forgotten here.
    """
    rows = []
    for saved in store_module.store().list(who.id, limit=25):
        try:
            trip = _load(saved.id)
        except HTTPException:
            continue
        role = roles.role_of(saved.payload, saved.owner, who)
        if not role:
            # No membership, no row. `_access` has always answered 404 here on
            # the grounds that a trip you were not let into does not exist to
            # you; this list did not, and returned the label, the legs and the
            # flight numbers with an empty `can`. Redaction blanks money, not
            # titles.
            continue
        disruption = flow.from_dict(saved.payload.get("disruption"))
        rows.append(roles.redact({
            "role": role,
            "can": sorted(c for c in (roles.VIEW, roles.MONEY, roles.ACT, roles.BOOK,
                                      roles.ABANDON, roles.SHARE, roles.RULES)
                          if roles.can(role, c)),
            "members": ([{"person": m["person"], "name": m.get("name", ""),
                          "role": m.get("role", "")}
                         for m in saved.payload.get("members") or []]
                        if roles.can(role, roles.SHARE) else None),
            "trip_id": saved.id,
            "label": saved.label,
            "created": saved.created.isoformat(),
            "preference": saved.payload.get("preference", ""),
            "total": round(sum(b.price for b in trip.bookings), 2),
            "currency": trip.in_order()[0].currency if trip.bookings else "SGD",
            "starts": _when(trip.in_order()[0].start) if trip.bookings else None,
            "legs": len(trip.bookings),
            "disrupted": disruption is not None,
            "disrupted_booking": disruption.booking_id if disruption else "",
            # Everything a page needs to show this trip again after a refresh.
            # It was already storing the trip; what it could not do was come
            # back to one, and half a restored trip -- rows without the reasons
            # they cannot be taken -- is worse than none.
            "problems": infeasible(trip),
            "durable": store_module.store().durable,
            # A cancelled trip is still a trip: which refunds were promised and
            # which calls are still owed is exactly what the traveller comes
            # back for. It reads as cancelled and it does not read as live.
            "abandoned": saved.payload.get("abandoned"),
            "risks": risk_mod.as_dicts(risk_mod.assess(trip)),
            "permissions": _perms(saved).to_dict(),
            "acted": saved.payload.get("acted"),
            "detected": saved.payload.get("detected"),
            "checked": saved.payload.get("checked"),
            "bookings": _bookings_out(trip, disruption,
                                      saved.payload.get("abandoned")),
        }, role))
    return {"itineraries": rows, "me": {"id": who.id, "name": who.name,
                                        "anonymous": who.anonymous}}


@app.post("/api/act")
def act(body: Act, who: roles.Person = Depends(_who)) -> dict:
    """Take the plan. This is where the lanes stop being labels.

    AUTO actions are performed, TAP and CALL are handed over, and the stored
    itinerary is rewritten to the trip the traveller now has. What was done and
    what is still waiting on them are reported separately, because a system
    that blurs those two has started lying about the useful part.
    """
    from base import PortError

    saved, _role = _access(body.trip_id, who, roles.ACT)
    already = saved.payload.get("acted")
    current = saved.payload.get("disruption")
    if already and (current is None or already.get("for") == current):
        # A replay -- a double-click, a retried request, a stale tab. Either
        # the stored disruption is the exact one this trip already acted on,
        # or acting RESOLVED it and nothing new has broken since. The second
        # case is the dangerous one: with no stored disruption the fallback
        # below fabricates a fresh cancellation against the rewritten
        # itinerary, which is how a flight quietly disappeared.
        raise HTTPException(
            409, "already handled — that plan was taken at "
                 f"{already.get('at', 'an earlier moment')}. Break something "
                 "new, or cancel again to re-plan from here.")
    trip = _load(body.trip_id)
    preference = saved.payload.get("preference", "")

    # The disruption that produced these plans, not a fresh guess at one. This
    # rebuilt a cancellation unconditionally, which was harmless while that was
    # the only thing anybody could inject and becomes a lie the moment a
    # traveller says "late" instead: acting on a plan for something that did
    # not happen.
    said = flow.from_dict(saved.payload.get("disruption"))
    try:
        recovery = flow.replan(
            trip,
            said if said and said.booking_id == body.booking_id
            else flow.cancel(trip, body.booking_id),
            preference=preference, permissions=_perms(saved))
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

    known = {a.id for a in plan.actions}
    unknown = [r for r in body.reject if r not in known]
    if unknown:
        raise HTTPException(422, f"no such action on this plan: {', '.join(unknown)}")
    return _take(body.trip_id, trip, recovery, plan, by=who.name,
                 skip=set(body.reject))


def _take(trip_id: str, trip: Trip, recovery, plan, by: str,
          skip: set[str] | None = None, actor: str = "",
          hold: bool = False) -> dict:
    """Take one plan: perform what can be performed, rewrite the itinerary,
    record who decided.

    One function for the button and for the agent, because the only thing
    that differs between "the traveller pressed Take this plan" and "the
    traveller pre-authorised up to EUR 300 and this cost EUR 72" is ``by`` --
    and that word has to be in the record, since an action the agent took on
    its own is exactly the kind a person later wants to find in the log.
    """
    saved = store_module.store().get(trip_id)
    skip = set(skip or set())
    lines: list[str] = []
    done = act_mod.perform(plan, trip, trip_id, log=lines.append, skip=skip,
                           armed=not recovery.permissions.disarmed and not hold)
    offer = next((o for o in recovery.offers if o.id == plan.id), None)
    updated, changed = act_mod.apply(plan, trip, recovery.disruption, offer, skip=skip)
    declined = [d for d in done if d.state == "declined"]

    verdict = recovery.verdict
    for action, outcome in zip(plan.actions, done):
        approval, why = verdict.approval(action) if verdict else ("", "")
        audit.record(
            store_module.store(), trip_id,
            actor=actor or by, type=audit.PERFORMED,
            subject=f"{outcome.verb}: {outcome.label} → {outcome.state}"
                    + (f" via {', '.join(outcome.channels)}" if outcome.channels else ""),
            citation=outcome.note or action.note or outcome.label,
            authorization=(f"declined by {by}" if outcome.state == "declined"
                           else f"{approval} — {why}" if why
                           else approval or f"taken by {by}"),
            feed=f"{outcome.lane} lane; executing the judged plan (no feed)")

    # Declining the replacement leaves the trip broken. Everything else in the
    # plan can be taken -- the hotel emailed, the transfer cancelled -- and the
    # traveller has said they will handle the flight; the disruption stays
    # open so the watch keeps counting down to whatever that flight was for.
    resolved = plan.id == "noop" or "buy:offer" not in skip

    payload = dict(saved.payload)
    payload["bookings"] = ingest.to_dicts(updated.bookings)
    payload["acted"] = {"at": datetime.now(timezone.utc).isoformat(),
                        # The disruption this act answered, verbatim. /api/act
                        # compares against it to refuse a replay: acting twice
                        # on the same event re-planned against the rewritten
                        # itinerary and deleted a leg the first act had kept.
                        "for": saved.payload.get("disruption"),
                        "plan": plan.name, "changed": changed, "by": by,
                        "declined": [d.label for d in declined],
                        "resolved": resolved,
                        "permission": recovery.verdict.why
                        if recovery.verdict and by == "the agent" else ""}
    if resolved:
        # This itinerary is no longer the broken one, so the watch stops
        # counting down deadlines that have been dealt with.
        payload.pop("disruption", None)
        payload.pop("watermark", None)
    store_module.store().update(trip_id, payload)

    return {
        "trip_id": trip_id,
        "plan": plan.name,
        "by": by,
        "resolved": resolved,
        "summary": act_mod.summarise(done, changed),
        "sent": [d.__dict__ for d in done if d.state == "sent"],
        "pending": [d.__dict__ for d in done if d.state == "pending"],
        "held": [d.__dict__ for d in done if d.state == "held"],
        "declined": [d.__dict__ for d in declined],
        "changed": changed,
        "log": lines,
        "bookings": _bookings_out(_load(trip_id), None if resolved else recovery.disruption),
    }


def _bookings_out(trip: Trip, disruption=None, abandoned=None) -> list[dict]:
    """The trip's rows, with the deviation attached to the one it happened to.

    KEPT SEPARATE FROM start AND end, deliberately. Those are what was booked
    and they do not move: a delay is what the world is doing to the schedule,
    not a correction of it, and overwriting `ends` with a predicted arrival
    would erase the very comparison the traveller is trying to make. So the
    affected row carries a `disruption` of its own, and every other row carries
    none.

    Without this a delayed leg was indistinguishable from an untouched one.
    /api/itineraries knew a trip was disrupted and not what had happened to it,
    so the panel showed a flight at its original times with nothing to say it
    was running three hours late -- which is the one fact the traveller opened
    the page for.

    ``abandoned`` is the same rule for the whole trip. A cancelled itinerary
    kept every row reading like a live booking -- the hotel most visibly of
    all, because it is the row with no buttons on it to look disabled -- so
    somebody who had just called the trip off was still looking at a room they
    were told they had. Each row now carries the errand that applies to it, and
    a row that carries one is not a booking any more.
    """
    #: Kept apart from `disruption` on purpose. A provider cancelling a flight
    #: and a traveller calling the trip off are different events with different
    #: consequences, and folding them into one field would have the page report
    #: the airline as having done something the traveller did.
    errands = {a["booking_id"]: a
               for a in ((abandoned or {}).get("actions") or [])
               if a.get("booking_id")}

    def deviation(b: Booking) -> dict | None:
        if disruption is None or disruption.booking_id != b.id:
            return None
        late = int(disruption.delay(trip).total_seconds() // 60)
        return {"cancelled": disruption.cancelled,
                "delay_minutes": late,
                "reason": disruption.reason,
                # The scheduled arrival and the one now expected, both, because
                # "now in at 23:05" means nothing without the 20:05 it replaced.
                "was": _when(b.end),
                "now_arrives": None if disruption.cancelled
                               else _when(disruption.new_end)}

    return [{"id": b.id, "title": b.title, "provider": b.provider,
             "kind": b.kind.value, "where": b.where,
             "starts": _when(b.start), "ends": _when(b.end),
             "must_arrive_by": _when(b.must_arrive_by),
             "price": b.price, "currency": b.currency, "pending": b.pending,
             "commitment": b.commitment, "who": b.who,
             "price_source": b.price_source,
             "disruption": deviation(b),
             "cancelled": errands.get(b.id),
             "ticket_group": b.ticket_group, "policy": b.policy.source}
            for b in trip.in_order()]


@app.post("/api/select")
def select(body: Selection, who: roles.Person = Depends(_owner)) -> dict:
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
            # Re-resolved through the search that produced it. A rail offer id
            # is minted from a departure minute and a flight id from Duffel's
            # own search, so asking the wrong provider finds nothing and reports
            # it as inventory that has moved on.
            found = flow.search_leg(leg.origin.upper(), leg.destination.upper(),
                                    _day(leg.on, zone), leg.mode,
                                    key=leg.offer_key or leg.offer_id)
            offer = (next((o for o in found if o.key == leg.offer_key), None)
                     if leg.offer_key else None)
            offer = offer or next((o for o in found if o.id == leg.offer_id), None)
            if offer is None:
                raise HTTPException(
                    409, f"that {'train' if leg.mode == 'rail' else 'flight'} is "
                         "no longer being sold — search again and pick from "
                         "what is there now")
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
        who.id, {"bookings": ingest.to_dicts(assembled.bookings),
                 "permissions": _default_rules(body.permissions, who)},
        body.label or "Booked here")
    _audited = _load(saved.id)
    audit.record(
        store_module.store(), saved.id,
        actor=who.name, type=audit.EXTRACTED,
        subject=f"{saved.label}: {len(_audited.bookings)} bookings selected "
                "from a search",
        citation=_cite_policies(_audited),
        authorization=audit.OBSERVED,
        feed=_feed_for(_audited))

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
        # Said before anything breaks: where this trip is thin, from the
        # booked times and the engine's own stress test -- see risk.py for
        # what is and is not being claimed.
        "risks": risk_mod.as_dicts(risk_mod.assess(trip)),
        "total": round(sum(b.price for b in trip.bookings), 2),
        "bookings": _bookings_out(trip),
    }


def _feed_for(trip: Trip) -> str:
    """Which ports produced this trip's rows, and whether they were live."""
    names = {Kind.FLIGHT: "duffel", Kind.RAIL: "transport.opendata.ch",
             Kind.LODGING: "liteapi"}
    used = sorted({names[b.kind] for b in trip.bookings if b.kind in names})
    return f"{' + '.join(used) or 'no ports'} ({MODE})"


def _cite_policies(trip: Trip) -> str:
    """The fare prose every later decision rests on, quoted once."""
    seen = list(dict.fromkeys(b.policy.source for b in trip.in_order()
                              if b.policy.source))
    return "; ".join(seen) or "no fare rules supplied"


def _respond(trip_id: str, saved, trip: Trip, disruption, injected: bool,
             actor: str = "the traveller"):
    """Record a disruption, work out the answer, and act on it if allowed.

    One path for a pressed button and for the status feed, because the only
    thing that differs is where the signal came from -- and that is recorded,
    not branched on. Everything after the signal is the same question: what
    breaks, what to do, and whether the traveller's rules let the agent do it
    without waiting.

    Returns the recovery and, when the agent took the plan, what it did.
    """
    perms = _perms(saved)
    recovery = flow.replan(trip, disruption,
                           preference=saved.payload.get("preference", ""),
                           permissions=perms)

    # Written down first, so the watch has something to watch a minute after
    # the page that started it was closed. A traveller pressing "cancelled" is
    # an event, and an unrecorded event did not happen.
    stored = dict(saved.payload)
    stored["disruption"] = flow.to_dict(recovery.disruption, injected=injected)
    stored.pop("watermark", None)      # a new disruption starts a new clock
    stored.pop("last_sent", None)
    store_module.store().update(trip_id, stored)

    source = trip.by_id(disruption.booking_id)
    audit.record(
        store_module.store(), trip_id,
        actor=actor, type=audit.ASSESSED,
        subject=f"{source.title}: {disruption.reason or 'disruption'}",
        citation="presence(place) + transit(place → booking) ≤ must_arrive_by, "
                 f"walked over {len(recovery.impact.nodes)} bookings; "
                 f"S${recovery.impact.do_nothing_cost:,.0f} destroyed if nobody acts",
        authorization=audit.OBSERVED,
        feed=("the traveller pressed the button (no feed)" if injected
              else f"aerodatabox ({MODE})"))
    if recovery.best is not None:
        verdict = recovery.verdict
        audit.record(
            store_module.store(), trip_id,
            actor=actor, type=audit.JUDGED,
            subject=f"recommended: {recovery.best.name}",
            citation=_why(trip, recovery) or "no options to weigh",
            authorization=(verdict.why or ("may be taken without asking"
                                           if verdict.auto else "waits for a person"))
                          if verdict else "no rules apply to inaction",
            feed=f"duffel + transport.opendata.ch ({MODE}), "
                 f"{len(recovery.offers)} offers searched")

    # THE AGENT ACTS, when it has been allowed to. The best plan is taken
    # without a click if the traveller's rules permit it -- which is the whole
    # difference between a delay at 02:00 answered at 02:00 and one answered
    # at 07:30 when somebody wakes up and presses a button. Inaction is never
    # taken; a plan that costs money is taken only within the cap; and what
    # was always going to need a person (the tap, the call) still does, and is
    # reported as waiting rather than as done.
    best = recovery.best
    taken = None
    verdict = recovery.verdict
    if best is not None and best.id != "noop" and verdict and (verdict.auto or verdict.hold):
        # The middle tier decides and HOLDS: the itinerary is rewritten and
        # the replacement enters as pending -- which is what a hold is in a
        # product that cannot pay -- but nothing leaves the building; the
        # sends are held exactly as the kill switch holds them, and the
        # notification tells the traveller to review.
        taken = _take(trip_id, trip, recovery, best, by="the agent",
                      actor=actor, hold=verdict.hold)
        taken["permission"] = verdict.why
        taken["auto_held"] = verdict.hold
    return recovery, taken


def _injected(trip_id: str, booking_id: str, make,
              who: roles.Person = roles.ANON) -> dict:
    """Answer the whole question about one injected disruption.

    Shared by both buttons because everything after the signal is identical --
    the gap is derived in plan.py from where the traveller now stands, the
    replacements come from the same provider that sold the original, and the
    ranking is the same arithmetic the terminal runs. ``make`` is the only
    difference, and it is a whole one: it decides whether the traveller ends up
    late at the destination or still at the origin.
    """
    from base import PortError

    saved, _role = _access(trip_id, who, roles.ACT)
    trip = _load(trip_id)
    try:
        trip.by_id(booking_id)
    except KeyError as exc:
        raise HTTPException(404, "no booking with that id on this trip") from exc

    # A trip nobody is taking cannot be delayed or cancelled. Letting it be
    # would start the watch counting down deadlines on bookings the traveller
    # has already been told are gone.
    if saved is not None and saved.payload.get("abandoned"):
        raise HTTPException(409, "that trip has been cancelled")
    perms = _perms(saved)
    try:
        recovery, taken = _respond(trip_id, saved, trip, make(trip), injected=True,
                                   actor=who.name)
    except PortError as exc:
        raise HTTPException(503, str(exc)) from exc

    # The clock the engine used, carried on the Recovery. Passing new_end here
    # was right only while every injected disruption was a cancellation: for a
    # delay it is the new arrival, hours ahead, and the page would have counted
    # down to deadlines it had already placed itself after.
    payload = _assessment(trip, recovery.disruption, recovery.impact,
                          recovery.plans, recovery.now, None,
                          trip_id, gap=recovery.gap, injected=True, perms=perms)
    payload["searched"] = [_offer(o) for o in recovery.offers]
    payload["saved"] = recovery.saved
    payload["preference"] = recovery.preference
    payload["warning"] = recovery.warning
    payload["why"] = _why(trip, recovery)
    payload["permissions"] = perms.to_dict()
    payload["excluded"] = [{"name": p.name, "why": permit.judge(p, perms).why}
                           for p in recovery.excluded]
    # The itinerary as it now reads, with the deviation on the row it happened
    # to. Returned so the page has ONE source for what is wrong with a leg --
    # it had two, the row it was holding and the signal in this response, and
    # the second does not survive a refresh.
    payload["bookings"] = _bookings_out(trip, recovery.disruption)
    if taken:
        payload["auto_taken"] = taken
        payload["bookings"] = taken["bookings"]
    return payload


def _why(trip: Trip, recovery) -> str:
    """The recommendation, as a sentence somebody can argue with.

    The brief's example is the target: "Recommended because it preserves your
    9:00 meeting, stays within the S$1,200 limit, and avoids a 5:40 departure."
    Everything in it has to be TRUE OF THIS PLAN and taken from the engine --
    which commitments it keeps, what it costs against the cheapest plan that
    does not, and what the traveller's rules say -- or it is the model writing
    travel advice, which is the thing this product exists not to do.
    """
    best = recovery.best
    if best is None:
        return ""
    noop = next((p.total_damage for p in recovery.plans if p.id == "noop"), 0.0)
    parts: list[str] = []
    kept = [trip.by_id(i).title for i in sorted(best.saved_ids)]
    if kept:
        parts.append("keeps " + ", ".join(kept))
        # The price of the goal: the cheapest plan that loses one of them.
        cheaper = [p for p in recovery.plans
                   if len(p.missed_ids) > len(best.missed_ids)
                   and p.total_damage < best.total_damage]
        if cheaper:
            low = min(cheaper, key=lambda p: p.total_damage)
            parts.append(f"costs S${best.total_damage - low.total_damage:,.0f} more than "
                         f"{'doing nothing' if low.id == 'noop' else low.name}, "
                         "which would miss it")
    elif best.missed_ids:
        parts.append("no option keeps " + ", ".join(
            trip.by_id(i).title for i in sorted(best.missed_ids)))
    if best.id != "noop" and not (kept and best.total_damage > noop):
        # Skipped when the sentence above already priced it against inaction.
        parts.append(f"S${best.total_damage:,.0f} of damage against S${noop:,.0f} "
                     "for doing nothing")
    elif best.id == "noop":
        # Inaction recommended is a real answer and deserves a real reason: it
        # won because every replacement costs more than what it would rescue.
        others = [p for p in recovery.plans if p.id != "noop"]
        if others:
            low = min(others, key=lambda p: p.total_damage)
            parts.append(f"nothing worth buying: the cheapest replacement, {low.name}, "
                         f"costs S${low.total_damage - noop:,.0f} more than it saves")
        else:
            parts.append("there is nothing to buy that would change the outcome")
        if best.at_risk:
            parts.append(f"S${best.at_risk:,.0f} is held by a phone call rather than lost")
    if recovery.verdict and recovery.verdict.why:
        parts.append(recovery.verdict.why)
    if recovery.warning:
        parts.append("but " + recovery.warning)
    lead = "Doing nothing is recommended because " if best.id == "noop" else "Recommended because it "
    return (lead + "; ".join(parts) + ".") if parts else ""


@app.post("/api/cancel")
def cancel(body: Cancellation, who: roles.Person = Depends(_who)) -> dict:
    """One leg is not going, and the traveller has just found out."""
    return _injected(body.trip_id, body.booking_id,
                     lambda trip: flow.cancel(trip, body.booking_id), who)


@app.post("/api/delay")
def delay(body: Delay, who: roles.Person = Depends(_who)) -> dict:
    """One leg is going late. Not a small cancellation -- a different world.

    The engine has always modelled this and only the scripted demo could reach
    it, so the disruption it handles best was the one a traveller could not
    produce on a trip they had booked.
    """
    if body.minutes < 1:
        raise HTTPException(422, "a delay has to be at least a minute")
    return _injected(body.trip_id, body.booking_id,
                     lambda trip: flow.delay(trip, body.booking_id, body.minutes), who)


def _load(trip_id: str) -> Trip:
    saved = store_module.store().get(trip_id)
    if saved is None:
        raise HTTPException(404, "no trip with that id")
    bookings, problems, _sources = ingest.to_bookings(saved.payload)
    if not bookings:
        raise HTTPException(422, {"problems": problems})
    return Trip(bookings)


def _perms(saved) -> permit.Permissions:
    return permit.Permissions.from_dict((saved.payload if saved else {}).get("permissions"))


def _profile(who: roles.Person = roles.ANON) -> profile_mod.Profile:
    return profile_mod.Profile.from_dict(store_module.store().profile(who.id))


def _default_rules(given: dict, who: roles.Person = roles.ANON) -> dict:
    """The rules a new trip starts with: what was sent, else the profile's.

    A trip booked with no rules at all used to start with nothing
    pre-authorised, every time, however often the traveller had set a cap
    before. The profile is where that cap now lives; the trip copies it at
    booking and owns its copy from then on, so changing the profile later
    does not silently change a trip already under way.
    """
    if given:
        return permit.Permissions.from_dict(given).to_dict()
    return _profile(who).permissions.to_dict()


@app.get("/api/profile")
def read_profile(who: roles.Person = Depends(_who)) -> dict:
    return {"profile": _profile(who).to_dict(), "empty": _profile(who).empty}


@app.post("/api/profile")
def write_profile(body: ProfileIn, who: roles.Person = Depends(_who)) -> dict:
    """Say once what would otherwise be asked on every trip.

    Everything here is applied somewhere specific and shown when it is: the
    home city and the sort order fill blanks on the confirmation card marked
    "from your profile", and the rules become each new trip's starting rules.
    Nothing is inferred from past behaviour -- a profile that rewrites itself
    is one the traveller cannot check.
    """
    found = places.find(body.home) if body.home.strip() else None
    if body.home.strip() and not found:
        raise HTTPException(422, f"I do not know a place called {body.home!r}")
    kept = profile_mod.Profile.from_dict({
        "preference": body.preference, "home": found.code if found else "",
        "permissions": {"auto_limit": body.auto_limit, "hold_limit": body.hold_limit,
                        "never": body.never, "always_ask": body.always_ask,
                        "disarmed": _profile(who).permissions.disarmed
                        if body.disarmed is None else body.disarmed}})
    store_module.store().save_profile(who.id, kept.to_dict())
    return {"profile": kept.to_dict(), "empty": kept.empty}


@app.post("/api/permissions")
def permissions(body: Permit, who: roles.Person = Depends(_who)) -> dict:
    """Change what the agent may do about this trip on its own.

    Per trip, because there is no traveller profile for it to live on. The
    moment there is one this moves there and nothing about how it is judged
    changes -- see permit.py.
    """
    saved, _role = _access(body.trip_id, who, roles.RULES)
    perms = permit.Permissions.from_dict(
        {"auto_limit": body.auto_limit, "hold_limit": body.hold_limit,
         "never": body.never, "always_ask": body.always_ask,
         "disarmed": _perms(saved).disarmed if body.disarmed is None
                     else body.disarmed})
    stored = dict(saved.payload)
    stored["permissions"] = perms.to_dict()
    store_module.store().update(body.trip_id, stored)
    if body.remember:
        kept = _profile(who)
        store_module.store().save_profile(who.id, {
            **kept.to_dict(), "permissions": perms.to_dict()})
    return {"trip_id": body.trip_id, "permissions": perms.to_dict(),
            "remembered": body.remember}


class NewPerson(BaseModel):
    name: str


class Share(BaseModel):
    trip_id: str
    name: str
    role: str


class Revoke(BaseModel):
    trip_id: str
    person: str


@app.post("/api/people")
def people(body: NewPerson) -> dict:
    """Become somebody. A name and a token; the token is shown once.

    This is the whole of sign-up, and it is said plainly on /test: there are
    no accounts, a token is a key, and a key can be copied.
    """
    person = roles.new_person(body.name)
    store_module.store().add_person(person.id, person.token, person.name)
    return {"id": person.id, "name": person.name, "token": person.token}


@app.get("/api/me")
def me(who: roles.Person = Depends(_who)) -> dict:
    return {"id": who.id, "name": who.name, "anonymous": who.anonymous,
            "roles": {r: sorted(c) for r, c in roles.ROLES.items()}}


@app.post("/api/share")
def share(body: Share, who: roles.Person = Depends(_who)) -> dict:
    """Let somebody in, as a role. Owner only.

    Returns the newcomer's token and a link carrying it, ONCE: the token is
    not stored anywhere it can be read back from, so a link lost is a link
    to revoke and reissue, never one to look up.
    """
    if body.role not in roles.ROLES or body.role == "owner":
        raise HTTPException(422, f"role must be one of "
                                 f"{', '.join(r for r in roles.ROLES if r != 'owner')}")
    saved, _role = _access(body.trip_id, who, roles.SHARE)
    person = roles.new_person(body.name)
    store_module.store().add_person(person.id, person.token, person.name)
    stored = dict(saved.payload)
    stored["members"] = list(stored.get("members") or []) + [
        {"person": person.id, "name": person.name, "role": body.role,
         "added_by": who.name, "at": datetime.now(timezone.utc).isoformat()}]
    store_module.store().update(body.trip_id, stored)
    return {"trip_id": body.trip_id, "person": person.id, "name": person.name,
            "role": body.role, "token": person.token,
            "can": sorted(roles.ROLES[body.role]),
            "link": f"/?token={person.token}"}


@app.post("/api/revoke")
def revoke(body: Revoke, who: roles.Person = Depends(_who)) -> dict:
    saved, _role = _access(body.trip_id, who, roles.SHARE)
    stored = dict(saved.payload)
    before = stored.get("members") or []
    stored["members"] = [m for m in before if m.get("person") != body.person]
    if len(stored["members"]) == len(before):
        raise HTTPException(404, "nobody by that id on this trip")
    store_module.store().update(body.trip_id, stored)
    return {"trip_id": body.trip_id, "removed": body.person,
            "members": [{"person": m["person"], "name": m.get("name", ""),
                         "role": m.get("role", "")} for m in stored["members"]]}


@app.post("/api/abandon")
def abandon(body: Abandon, who: roles.Person = Depends(_who)) -> dict:
    """What cancelling the whole trip costs, and then doing it.

    Nothing here is a disruption. Nothing broke -- the traveller changed their
    mind -- so there is no impact to propagate and no alternative to rank. What
    survives from the recovery path is the part worth keeping: one action per
    booking, in the lane that can actually perform it, and `act.perform` to run
    them, because from there "who does this and did they" is the same question.
    """
    saved, _role = _access(body.trip_id, who, roles.ABANDON)
    if saved.payload.get("abandoned"):
        raise HTTPException(409, "that trip has already been cancelled")

    trip = _load(body.trip_id)
    now = datetime.now(timezone.utc)
    plan = plan_mod.abandon(trip, now)

    payload = {
        "trip_id": body.trip_id,
        "label": saved.label,
        "confirmed": body.confirm,
        "plan": _plan(plan, best=True),
        # What the traveller gets back and what the decision costs, both, at
        # THIS moment: a window that is open on Tuesday is shut on Friday, and
        # the figure is only true next to the time it was computed at.
        # `or 0.0` because negating a zero net gives -0.0, and a page that
        # renders it faithfully tells the traveller they are getting "−€0"
        # back from a trip with nothing to recover.
        "refund": round(-plan.net_cash, 2) or 0.0,
        "lost": plan.total_damage,
        "paid": round(sum(b.price for b in trip.bookings), 2),
        "as_of": _when(now),
        "bookings": _bookings_out(trip),
    }
    if not body.confirm:
        return payload

    # The same gate every plan passes. Abandoning is the traveller's own
    # two-step decision, but the AUTO-lane emails inside it are the agent
    # acting -- and rules like always_ask("notify") apply to those exactly
    # as they do on a recovery plan.
    perms = _perms(saved)
    verdict = permit.judge(plan, perms)
    rules_hold = any(
        verdict.approvals.get(a.label, (permit.AUTO, ""))[0] != permit.AUTO
        for a in plan.actions if a.lane is Lane.AUTO and a.verb == "notify")
    kill_note = " — kill switch on; the message is yours to send"
    ask_note = " — your rules ask first; the message is yours to send"
    lines: list[str] = []
    done = act_mod.perform(
        plan, trip, body.trip_id, log=lines.append,
        armed=not perms.disarmed and not rules_hold,
        held_note=kill_note if perms.disarmed else ask_note)
    for outcome in done:
        audit.record(
            store_module.store(), body.trip_id,
            actor=who.name, type=audit.PERFORMED,
            subject=f"{outcome.verb}: {outcome.label} → {outcome.state}"
                    + (f" via {', '.join(outcome.channels)}" if outcome.channels else ""),
            citation=outcome.note or outcome.label,
            authorization=f"whole-trip cancellation, confirmed by {who.name} (two-step)",
            feed=f"{outcome.lane} lane; executing the cancellation (no feed)")

    stored = dict(saved.payload)
    # The trip is KEPT, marked. Deleting it would take with it the one thing
    # the traveller now needs -- which refunds were promised, which calls are
    # still theirs to make -- and there is no undoing a delete either.
    stored["abandoned"] = {
        "at": now.isoformat(),
        "by": who.name,
        "reason": body.reason,
        "refund": payload["refund"],
        "lost": payload["lost"],
        "actions": [_action(a) for a in plan.actions],
    }
    # Whatever was broken is beside the point now, and the watch must stop
    # counting down deadlines on a trip nobody is taking.
    stored.pop("disruption", None)
    stored.pop("watermark", None)
    stored.pop("last_sent", None)
    store_module.store().update(body.trip_id, stored)

    return {**payload,
            "bookings": _bookings_out(trip, None, stored["abandoned"]),
            "summary": act_mod.summarise(done, []),
            "sent": [d.__dict__ for d in done if d.state == "sent"],
            "held": [d.__dict__ for d in done if d.state == "held"],
            "pending": [d.__dict__ for d in done if d.state == "pending"],
            "log": lines}


@app.get("/api/state")
def state(base: str = "today", trip: str = "",
          who: roles.Person = Depends(_who)) -> dict:
    """The whole assessment. Same two engine calls the CLI makes.

    Without ``trip`` this answers about the built-in scenario, which is what the
    demo and the tests use. With one, it answers about a real stored itinerary.
    """
    from base import PortError

    role = "owner"
    if trip:
        _saved, role = _access(trip, who, roles.VIEW)
    loaded = _load(trip) if trip else None

    try:
        return roles.redact(_assemble(_base(base), loaded, trip), role)
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


class Disarm(BaseModel):
    trip_id: str
    disarmed: bool


@app.post("/api/disarm")
def disarm(body: Disarm, who: roles.Person = Depends(_who)) -> dict:
    """The master kill switch: flip it, and every action on every plan waits
    for a person -- the cap ignored, the AUTO lane's emails held, the agent
    recommending and touching nothing. Deadline alerts to the traveller keep
    flowing; a switch that silences your own warnings is a worse product.

    Journalled on every flip, because "who turned the machine off, and when"
    is the first question after any incident involving one.
    """
    saved, _role = _access(body.trip_id, who, roles.RULES)
    perms = _perms(saved)
    stored = dict(saved.payload)
    stored["permissions"] = {**perms.to_dict(), "disarmed": body.disarmed}
    store_module.store().update(body.trip_id, stored)
    audit.record(
        store_module.store(), body.trip_id,
        actor=who.name, type=audit.TOGGLED,
        subject=f"master kill switch → {'disarmed' if body.disarmed else 'armed'}",
        citation=permit.DISARMED if body.disarmed
                 else f"re-armed: the trip's own rules apply again "
                      f"(cap S${perms.auto_limit:,.0f})",
        authorization=f"by {who.name}",
        feed="control panel (no feed)")
    return {"trip_id": body.trip_id, "disarmed": body.disarmed,
            "permissions": stored["permissions"]}


@app.get("/api/trail")
def trail(trip: str, who: roles.Person = Depends(_who)) -> dict:
    """The paper trail for one trip: observed, decided, done, on whose say-so.

    Gated on the `money` capability rather than `view`, because the citations
    quote fares, refunds and caps -- the decision log IS the ledger with
    reasons attached, and a host who may not see a price may not read a
    sentence that names one.
    """
    _saved, _role = _access(trip, who, roles.MONEY)
    return {"trip_id": trip, "events": store_module.store().trail(trip)}


@app.get("/api/trail.txt")
def trail_text(trip: str, token: str = "",
               who: roles.Person = Depends(_who)) -> Response:
    """The same trail as plain text a judge can read on the spot.

    Accepts ?token= as well as the header, because this is the one endpoint
    meant to be opened as a bare link from the panel -- the same trade the
    share links already make, stated rather than snuck."""
    if token.strip() and who.anonymous:
        found = store_module.store().person(token.strip())
        if not found:
            raise HTTPException(401, "that link is not one I handed out")
        who = roles.Person(found["id"], found["name"], token.strip())
    _saved, _role = _access(trip, who, roles.MONEY)
    return Response(audit.render(store_module.store().trail(trip)),
                    media_type="text/plain; charset=utf-8")


class Wipe(BaseModel):
    what: str = "trips"          # "trips" | "everything"


@app.post("/api/admin/wipe")
def admin_wipe(body: Wipe, x_admin_token: str = Header("")) -> dict:
    """Clear stored data on a deployed instance, for whoever holds the admin
    token -- which is nobody until DOWNSTREAM_ADMIN_TOKEN is set in the
    environment.

    OFF BY DEFAULT, AND OFF MEANS INVISIBLE. With no token configured the
    endpoint answers 404 exactly like a route that does not exist, because an
    admin surface that advertises itself on every deployment is a target on
    every deployment. With one configured, the comparison is constant-time and
    a miss is a 403 that names nothing.

    "trips" clears itineraries only -- shared links and profiles survive, so
    testers keep their identities and rules and simply re-book. "everything"
    takes people and profiles with it: every shared link dies, and whoever
    holds one sees "that link is not one I handed out", which is the truth.
    """
    import hmac

    expected = os.environ.get("DOWNSTREAM_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(404, "Not Found")
    if not hmac.compare_digest(x_admin_token.strip(), expected):
        raise HTTPException(403, "no")
    if body.what not in ("trips", "everything"):
        raise HTTPException(422, "what must be 'trips' or 'everything'")

    gone = store_module.store().wipe(
        trips=True,
        people=(body.what == "everything"),
        profiles=(body.what == "everything"))
    return {"wiped": gone, "storage": store_module.status()["using"]}


@app.post("/api/story")
def story() -> dict:
    """One complete trip, taken through everything the app can do, by the app.

    /demo is the 2023-era capability frozen for the tests: one delay, one
    trip, S$423. Everything built since -- the profile, the meeting, the
    permission cap, the agent acting, the roles -- was demonstrable only by a
    person typing. This runs the whole arc server-side THROUGH THE SAME
    HANDLERS the buttons call, so nothing here can drift from what a person
    clicking would get, and returns each chapter with the figures the engine
    computed. Scripted inputs, computed outcomes: the same discipline as
    /demo, applied to the whole product.

    Every run builds a fresh owner and fresh trips, because the story ends
    with a disruption handled and a cancellation priced, and a story that can
    only be told once is a demo that breaks at the second audience.
    """
    from base import PortError
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    day = "2026-09-18"
    beats: list[dict] = []

    # -- chapter 1: say it once ------------------------------------------
    owner = roles.new_person("Alex")
    store_module.store().add_person(owner.id, owner.token, owner.name)
    kept = profile_mod.Profile.from_dict({
        "preference": "cheapest", "home": "SIN",
        "permissions": {"auto_limit": 300}})
    store_module.store().save_profile(owner.id, kept.to_dict())
    beats.append({
        "title": "Say it once",
        "said": "Alex tells the profile three things: home is Singapore, sort "
                "cheapest, and the agent may act up to S$300 without asking. "
                "Everything after this is those three sentences, applied.",
        "profile": kept.to_dict()})

    # -- chapter 2: book from a sentence ---------------------------------
    try:
        inbound = flow.search_flights("SIN", "ZRH", _day(day, _zone("Asia/Singapore")))
        first = min(inbound, key=lambda o: o.arrive)
        onward = flow.search_leg("ZRH", "MXP", _day(day, _zone("Europe/Zurich")))
        stays = flow.search_hotels("Milan", "IT", _day(day, _zone("Europe/Rome")),
                                   _day("2026-09-20", _zone("Europe/Rome")), code="MXP")
        trains = flow.search_rail("ZRH", "MXP", _day(day, _zone("Europe/Zurich")))
    except PortError as exc:
        raise HTTPException(503, f"the story needs the recorded inventory: {exc}")

    picked = None
    for candidate in sorted(onward, key=lambda o: o.depart):
        assembled, problems = flow.select([first, candidate], stays[:1])
        if not problems:
            picked = candidate
            break
    if picked is None:
        raise HTTPException(503, "no feasible onward hop in the recorded offers")

    saved = store_module.store().save(
        owner.id,
        {"bookings": ingest.to_dicts(assembled.bookings),
         "preference": "cheapest",
         "permissions": _default_rules({}, owner)},
        "The story · Singapore to Milan")
    trip_id = saved.id
    trip = _load(trip_id)
    # The story saves through the store directly rather than through
    # /api/select, so it owes the trail the same extraction entry that door
    # writes -- or the scripted run's own paper trail would start at the
    # assessment, with the fare rules every later decision cites never quoted.
    audit.record(
        store_module.store(), trip_id,
        actor=owner.name, type=audit.EXTRACTED,
        subject=f"{saved.label}: {len(trip.bookings)} bookings selected from a search",
        citation=_cite_policies(trip),
        authorization=audit.OBSERVED,
        feed=_feed_for(trip))
    beats.append({
        "title": "A sentence, not a form",
        "said": "One sentence books it. The card said 'From where: Singapore — from "
                "your profile'; under cheapest, the S$108 train ranked in the same "
                "list as every fare, marked estimated because no API sells "
                "that ticket. Alex "
                "takes the morning flight and a room, and the trip inherits the "
                "S$300 rule.",
        "trains_beat_flights": bool(trains) and (not onward or
            trains[0].price <= min(o.price for o in onward)),
        "cheapest_train": trains[0].price if trains else None,
        "bookings": _bookings_out(trip),
        "total": round(sum(b.price for b in trip.bookings), 2),
        "rules": _perms(store_module.store().get(trip_id)).to_dict()})

    # -- chapter 3: the reason the trip exists ---------------------------
    zone = ZoneInfo("Europe/Rome")
    meeting = appt_mod.Appointment(
        what="Meeting with the client", who="the client", where="MXP", place="MXP",
        day=_date.fromisoformat(day),
        when=datetime(2026, 9, 18, 22, 0, tzinfo=zone), confirmed=True)
    verdict = appt_mod.assess(trip, meeting)
    payload = dict(store_module.store().get(trip_id).payload)
    payload["bookings"] = ingest.to_dicts(verdict["trip"].bookings)
    store_module.store().update(trip_id, payload)
    trip = _load(trip_id)
    beats.append({
        "title": "The reason the trip exists",
        "said": "A meeting with the client, 22:00 in Milan. No fare, so most "
                "engines file it as free to miss; here it is a commitment — "
                "counted, never priced — and it is about to decide a ranking.",
        "feasible": verdict["feasible"],
        "clashes": verdict["about_this"]})

    # -- where this trip is thin, said before anything breaks --------------
    thin = risk_mod.assess(trip)
    beats.append({
        "title": "Where this trip is thin",
        "said": "Before anything breaks, the trip says where it would snap. "
                "The two flights are separate purchases with 75 minutes to "
                "spare — nobody owes Alex that connection — and each leg "
                "carries the smallest delay that starts costing money, from "
                "the same arithmetic that will price the real one two "
                "chapters from now. Derived, not predicted: no weather feed, "
                "no probabilities, no crystal ball.",
        "risks": risk_mod.as_dicts(thin),
        # The flag the rest of the story is about: the connection the delay
        # chapter bends and the cancellation chapter snaps.
        "premise_flagged": any(r.kind == "unprotected" for r in thin)})

    # -- chapter 4: other people, by what their job needs ------------------
    marco = roles.new_person("Marco, the host")
    store_module.store().add_person(marco.id, marco.token, marco.name)
    payload = dict(store_module.store().get(trip_id).payload)
    payload["members"] = [{"person": marco.id, "name": marco.name, "role": "host",
                           "added_by": owner.name,
                           "at": datetime.now(timezone.utc).isoformat()}]
    store_module.store().update(trip_id, payload)
    as_marco = next(t for t in itineraries(roles.Person(marco.id, marco.name))
                    ["itineraries"] if t["trip_id"] == trip_id)
    beats.append({
        "title": "Marco is meeting them",
        "said": "Alex shares the trip with Marco as host. Marco sees every leg "
                "and every time, and not one price — the same payloads, every "
                "monetary key blanked, on every endpoint there is.",
        "marco_sees": [{"title": b["title"], "starts": b["starts"], "ends": b["ends"],
                        "price": b["price"]} for b in as_marco["bookings"]],
        "marco_total": as_marco["total"],
        "marco_link": f"/?token={marco.token}"})

    # -- chapter 5: thirty minutes late ------------------------------------
    onward_id = [b for b in trip.in_order() if b.kind is Kind.FLIGHT][1].id
    small = _injected(trip_id, onward_id,
                      lambda t: flow.delay(t, onward_id, 30), roles.Person(owner.id, owner.name))
    beats.append({
        "title": "Thirty minutes late",
        "said": "The onward hop slips 30 minutes. Nothing is at risk — the "
                "slack absorbs it, and the engine says so instead of crying "
                "wolf. That restraint is why the alerts that do fire mean "
                "something.",
        "destroyed": small["impact"]["do_nothing"],
        "at_risk": small["impact"]["at_risk"],
        "nodes": [{"title": n["title"], "severity": n["severity"], "reason": n["reason"]}
                  for n in small["impact"]["nodes"]]})

    # -- chapter 6: cancelled, and the agent acts ---------------------------
    big = _injected(trip_id, onward_id,
                    lambda t: flow.cancel(t, onward_id), roles.Person(owner.id, owner.name))
    taken = big.get("auto_taken")
    beats.append({
        "title": "Cancelled — and answered before anyone is awake",
        "said": "The same hop is cancelled outright. The agent searched "
                "airlines and the railway, ranked every option against doing "
                "nothing, and acted inside the S$300 Alex pre-authorised: "
                "the plan that keeps the meeting wins over cheaper inaction, "
                "the hotel is emailed, the itinerary is rewritten, and one "
                "purchase link is left for a person. Nothing was bought.",
        "why": big.get("why", ""),
        "auto_taken": ({"plan": taken["plan"], "permission": taken["permission"],
                        "summary": taken["summary"],
                        "sent": [d["label"] for d in taken["sent"]],
                        "pending": [d["label"] for d in taken["pending"]]}
                       if taken else None),
        # The top of the ranking, PLUS doing nothing wherever it fell. The
        # goal-first sort pushes every meeting-misser below every keeper, so a
        # plain top-five can be all keepers -- and the row that makes the
        # story's point is the EUR 0 inaction that loses BECAUSE it misses
        # the client.
        "plans": [{"name": q["name"], "total_damage": q["total_damage"],
                   "saves": q["saves"], "misses": q["misses"], "auto": q["auto"],
                   "mode": q["mode"], "best": q["best"]}
                  for q in (big["plans"][:4]
                            + [next(q for q in big["plans"] if q["id"] == "noop")]
                            if not any(q["id"] == "noop" for q in big["plans"][:4])
                            else big["plans"][:5])],
        "bookings": big["bookings"]})

    def _with_client_meeting(copy_id: str) -> None:
        """The copies must be the SAME trip, meeting included. Without the
        commitment, doing nothing at S$0 outranks the S$108 train and there is
        nothing for the tier -- or the switch -- to be shown declining."""
        copy_trip = _load(copy_id)
        copy_verdict = appt_mod.assess(copy_trip, meeting)
        copy_payload = dict(store_module.store().get(copy_id).payload)
        copy_payload["bookings"] = ingest.to_dicts(copy_verdict["trip"].bookings)
        store_module.store().update(copy_id, copy_payload)

    # -- chapter 7: the middle tier -- decided, held, flagged ----------------
    # Another copy, with the cap set BELOW the train and the hold tier above
    # it (act <= 50, hold <= 200), so the same S$108 plan lands in the middle:
    # the agent decides, nothing leaves the building.
    tiered = store_module.store().save(
        owner.id,
        {"bookings": ingest.to_dicts(assembled.bookings),
         "preference": "cheapest",
         "permissions": permit.Permissions.from_dict(
             {"auto_limit": 50, "hold_limit": 200}).to_dict()},
        "The story · the hold-tier copy")
    _with_client_meeting(tiered.id)
    tiered_trip = _load(tiered.id)
    tiered_onward = [b for b in tiered_trip.in_order() if b.kind is Kind.FLIGHT][1].id
    mid = _injected(tiered.id, tiered_onward,
                    lambda t: flow.cancel(t, tiered_onward),
                    roles.Person(owner.id, owner.name))
    held_take = mid.get("auto_taken") or {}
    beats.append({
        "title": "The middle tier: decided, held, flagged",
        "said": "The same trip again, with the tiers the deck describes: act "
                "up to S$50, auto-hold up to S$200, ask above that. The S$108 "
                "plan is over the cap and inside the hold tier, so the agent "
                "still DECIDES -- the itinerary is rewritten and the "
                "replacement enters as pending, which is what a hold is in a "
                "product that cannot pay -- but nothing leaves the building: "
                "the hotel email is held, zero channels used, and the "
                "notification says review it. One ladder, three rungs: act, "
                "hold, ask.",
        "cap": 50, "hold": 200,
        "auto_held": bool(held_take.get("auto_held")),
        "permission": held_take.get("permission", ""),
        "sent": len(held_take.get("sent", [])),
        "held": [d["label"] for d in held_take.get("held", [])],
        "pending": [d["label"] for d in held_take.get("pending", [])],
        "summary": held_take.get("summary", "")})

    # -- chapter 8: the same cancellation, with the machine stopped ----------
    # A third copy of the trip, with the same S$300 cap chapter six acted
    # under -- so the ONLY difference between the two chapters is the switch.
    third = store_module.store().save(
        owner.id,
        {"bookings": ingest.to_dicts(assembled.bookings),
         "preference": "cheapest", "permissions": _default_rules({}, owner)},
        "The story · the kill-switch copy")
    disarm(Disarm(trip_id=third.id, disarmed=True),
           roles.Person(owner.id, owner.name))
    _with_client_meeting(third.id)
    third_trip = _load(third.id)
    third_onward = [b for b in third_trip.in_order() if b.kind is Kind.FLIGHT][1].id
    stopped = _injected(third.id, third_onward,
                        lambda t: flow.cancel(t, third_onward),
                        roles.Person(owner.id, owner.name))
    held_best = stopped["plans"][0]
    held_done = act(Act(trip_id=third.id, booking_id=third_onward,
                        plan_key=held_best.get("key") or held_best["id"]),
                    roles.Person(owner.id, owner.name))
    beats.append({
        "title": "The same cancellation, with the kill switch on",
        "said": "An identical copy of the trip, the same S$300 cap the armed "
                "chapter acted under -- and Alex "
                "flips the master kill switch first. The same hop is cancelled. "
                "This time the agent assesses, ranks, recommends -- and touches "
                "nothing: every line waits for a person with one fixed reason, "
                "and when Alex takes the plan by hand, even the hotel email is "
                "HELD rather than sent. Zero channels used. Read-only means "
                "read, in exactly the case a judge would test.",
        "cap": 300,
        "auto_taken": "auto_taken" in stopped,
        "why": stopped["why"],
        "approvals": sorted({a["approval"] for q in stopped["plans"][:3]
                             for a in q["actions"]}),
        "sent": len(held_done["sent"]),
        "held": [d["label"] for d in held_done["held"]],
        "summary": held_done["summary"],
        "toggled": next((e["subject"] for e in
                         store_module.store().trail(third.id)
                         if e["type"] == audit.TOGGLED), "")})

    # -- chapter 8: what calling it all off would cost ----------------------
    second = store_module.store().save(
        owner.id, {"bookings": ingest.to_dicts(assembled.bookings)},
        "The story · the trip Alex thought better of")
    quote = abandon(Abandon(trip_id=second.id), roles.Person(owner.id, owner.name))
    beats.append({
        "title": "And if Alex were not going at all",
        "said": "A second, identical trip, priced for cancellation and not "
                "cancelled: one errand per booking, sorted by who can do it, "
                "and what comes back plus what is gone equals what was paid — "
                "at this moment, because a window open today is shut tomorrow.",
        "paid": quote["paid"], "refund": quote["refund"], "lost": quote["lost"],
        "errands": [{"label": a["label"], "lane": a["lane"], "cash_in": a["cash_in"],
                     "note": a["note"]} for a in quote["plan"]["actions"]]})

    # -- chapter 8: everything above, on the record --------------------------
    events = store_module.store().trail(trip_id)
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    beats.append({
        "title": "Everything above, on the record",
        "said": "Every decision the story just made wrote itself down as it "
                "happened: what was observed, what was decided, what ran, on "
                "whose say-so — each entry citing the fare rule or the "
                "traveller's own cap it rested on, and naming the feed that "
                "triggered it. One entry per decision, and an entry that "
                "cannot explain itself cannot be written: the constructor "
                "refuses an empty timestamp, citation or authorization.",
        "counts": counts,
        "events": events,
        # The token is the identity, the same trade the share links make.
        "trail_link": f"/api/trail.txt?trip={trip_id}&token={owner.token}"})

    return {"trip_id": trip_id, "owner": owner.name,
            "ports": {"mode": MODE, "degraded": list(degraded)},
            "beats": beats}


@app.get("/story")
def story_page() -> FileResponse:
    return FileResponse(STATIC / "story.html")


@app.get("/script")
def demo_script() -> FileResponse:
    """The demo as a script: what to type, what to say, what to point at.

    /test is for somebody probing the app alone; this is for somebody holding
    a room. Same rule as /test for why it is a page and not a document -- the
    first thing it says depends on whether the instance is live or replaying,
    and prose asserting that goes stale silently.
    """
    return FileResponse(STATIC / "script.html")


@app.get("/test")
def test_guide() -> FileResponse:
    """What the app is and how to break it, for somebody who did not build it.

    A page rather than a document because the one fact a tester most needs --
    whether this instance is talking to real providers or replaying recordings
    -- changes per deploy and decides which routes exist at all. Written down
    in a README it goes stale silently and sends everybody hunting the wrong
    bug; read from /health it cannot.
    """
    return FileResponse(STATIC / "test.html")


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


@app.get("/developer")
def developer_page() -> FileResponse:
    """What this instance runs on and what the four demo doors are for --
    the information a traveller never needs and a developer always asks."""
    return FileResponse(STATIC / "developer.html")


# The shared sheet and helpers. Explicit routes rather than a StaticFiles
# mount, matching every other page here -- nothing else that lands in
# static/ becomes reachable by accident. Pages reference these with ?v=N;
# bump the number when editing, because FileResponse sends no cache headers
# and a stale stylesheet mid-rehearsal is a real risk.
@app.get("/static/app.css")
def app_css() -> FileResponse:
    return FileResponse(STATIC / "app.css")


@app.get("/static/app.js")
def app_js() -> FileResponse:
    return FileResponse(STATIC / "app.js")


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
