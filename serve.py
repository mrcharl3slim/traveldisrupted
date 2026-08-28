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

import _model                                                 # noqa: E402
import aerodatabox, duffel, rail                              # noqa: E402,E401
from base import MODE, degraded, shifted                      # noqa: E402
from demo_trip import DEFAULT_BASE, anchor, build_trip        # noqa: E402
from graph import propagate                                   # noqa: E402
from plan import Lane, generate                               # noqa: E402

STATIC = ROOT / "static"
app = FastAPI(title="downstream")

LANE_KEY = {Lane.AUTO: "auto", Lane.TAP: "tap", Lane.CALL: "call"}


def _base(raw: str) -> date | None:
    """today | +N | -N | YYYY-MM-DD | "" -> the trip exactly as written.

    Live flight status covers about a week either side of today, so a scenario
    pinned to October cannot be shown live in September. Anchoring to run time
    is what makes "live" checkable rather than asserted.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw == "today":
        return date.today()
    if raw[0] in "+-":
        return date.today() + timedelta(days=int(raw))
    try:
        return date.fromisoformat(raw)
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


def _assemble(basis: date | None) -> dict:
    trip = build_trip(basis)
    now = anchor(basis, 12, 2, 38)

    disruption = aerodatabox.disruption("SQ346", anchor(basis, 11, 9, 0), "sq346")
    offers = (duffel.offers("ZRH", "MXP", anchor(basis, 12, 9, 0), after=disruption.new_end)
              + rail.offers("Zurich HB", "Milano Centrale", anchor(basis, 12, 9, 0),
                            {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}))

    impact = propagate(trip, disruption, now)
    plans = generate(trip, disruption, now, offers)
    source = trip.by_id(disruption.booking_id)
    cutoff = impact.next_cutoff

    return {
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
        "fixtures": sorted(p.name for p in (ROOT / "fixtures").glob("*")),
    }


@app.get("/api/state")
def state(base: str = "") -> dict:
    """The whole assessment. Same two engine calls the CLI makes."""
    from base import PortError

    try:
        return _assemble(_base(base))
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
