"""The engine as MCP tools, so any agent can ask it questions.

The interesting claim is not "we built an agent" but "the replanning engine is
a tool other agents can call". Detection is commodity; cross-provider replanning
is not, and an MCP surface is how that becomes somebody else's building block
rather than our demo. An assistant holding a traveller's itinerary can ask what
one delay costs without knowing anything about fare rules or ground transit.

    python mcp_server.py          # stdio, replay-backed, no keys needed

Add to a client's config (Claude Desktop, Cursor, anything speaking MCP):

    {"mcpServers": {"downstream": {
        "command": "python", "args": ["/absolute/path/to/mcp_server.py"]}}}

Every tool takes ``base`` and defaults to today, because live flight status only
covers about a week either side of now.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from demo_trip import anchor, as_written, build_trip, resolve_base   # noqa: E402
from graph import propagate                   # noqa: E402
from plan import generate                     # noqa: E402
import aerodatabox, duffel, rail              # noqa: E402,E401

STATIONS = {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}


def _scene(base: str | None, now: str | None):
    """Trip, disruption and clock for one anchoring. Every tool starts here.

    Returning the disruption alongside the trip matters: they have to come from
    the same anchor or the engine compares a delay in October with an itinerary
    running next Tuesday and answers confidently about nothing.
    """
    basis = resolve_base(base)
    trip = build_trip(basis)
    when = datetime.fromisoformat(now) if now else anchor(basis, 12, 2, 38)
    return trip, basis, when


def impact_of(flight: str = "SQ346", base: str = "today",
              now: str | None = None) -> dict:
    """What a delay breaks, what it costs, and which deadline falls first.

    Args:
        flight: flight number to check, e.g. "SQ346".
        base: when the trip runs — "today", "written", "+3", or a date.
        now: the moment to judge from, ISO. Defaults to the scenario's own clock.
    """
    trip, basis, at = _scene(base, now)
    d = aerodatabox.disruption(flight, anchor(basis, 11, 9, 0), "sq346")
    if not d:
        return {"disrupted": False}
    imp = propagate(trip, d, at)
    when, node = imp.next_cutoff
    return {
        "disrupted": True,
        "anchored_to": trip.in_order()[0].start.date().isoformat(),
        "delay_minutes": int(d.delay(trip).total_seconds() // 60),
        "do_nothing_sgd": imp.do_nothing_cost,
        "recoverable_sgd": imp.act_now_value,
        "next_cutoff": {"at": when.isoformat(), "booking": node.booking.title},
        "nodes": [{"id": n.id, "title": n.booking.title, "severity": n.severity.value,
                   "exposure_sgd": n.exposure, "recoverable_sgd": n.recoverable,
                   "why": n.reason} for n in imp.nodes],
    }


def recovery_plans(flight: str = "SQ346", base: str = "today",
                   now: str | None = None) -> dict:
    """Ranked recovery options, cheapest total damage first.

    Each action carries the lane that can perform it: "auto" needs nobody,
    "tap" needs the traveller to authorise a payment, "call" has no consumer
    API at all. An agent must not present a "call" action as something it did.
    """
    trip, basis, at = _scene(base, now)
    d = aerodatabox.disruption(flight, anchor(basis, 11, 9, 0), "sq346")
    if not d:
        return {"disrupted": False}
    offers = (duffel.offers("ZRH", "MXP", anchor(basis, 12, 9, 0), after=d.new_end,
                              prefer=as_written(12))
              + rail.offers("Zurich HB", "Milano Centrale",
                            anchor(basis, 12, 9, 0), STATIONS))
    return {"plans": [{
        "id": p.id, "name": p.name,
        "net_cash_sgd": p.net_cash, "total_damage_sgd": p.total_damage,
        "arrives": p.arrives_at.isoformat() if p.arrives_at else None,
        "actions": [{"lane": a.lane.value, "label": a.label,
                     "out_sgd": a.cash_out, "in_sgd": a.cash_in,
                     # "estimate" means no reachable API quotes this fare and
                     # the number is ours. An agent repeating it must say so.
                     "price_source": a.price_source or "n/a",
                     "deadline": a.deadline.isoformat() if a.deadline else None}
                    for a in p.actions],
    } for p in generate(trip, d, at, offers)]}


def itinerary(base: str = "today") -> dict:
    """The trip itself: every booking, what it cost, and its deadline.

    Here so an agent can see what it is reasoning about before asking what a
    delay does to it.
    """
    trip, _basis, _at = _scene(base, None)
    return {"bookings": [{
        "id": b.id, "title": b.title, "provider": b.provider,
        "starts": b.start.isoformat(),
        "ends": b.end.isoformat() if b.end else None,
        "must_arrive_by": b.must_arrive_by.isoformat(),
        "where": b.where, "price": b.price, "currency": b.currency,
        # Two bookings sharing a ticket_group are one contract and the airline
        # owes a reaccommodation. Different groups mean nobody owes anything —
        # which is this trip's entire failure mode.
        "ticket_group": b.ticket_group,
    } for b in trip.in_order()]}


def main():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        sys.exit("pip install mcp   (this server is optional -- the engine, "
                 "the agent and the API do not depend on it)")
    server = FastMCP("downstream")
    server.tool()(itinerary)
    server.tool()(impact_of)
    server.tool()(recovery_plans)
    server.run()


if __name__ == "__main__":
    main()
