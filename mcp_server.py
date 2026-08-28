"""The engine as MCP tools, so any agent can ask it questions.

Sponsor-stack tick, and first on the cut list -- nothing else depends on it.
It exists because the interesting claim is not "we built an agent" but "the
replanning engine is a tool other agents can call": detection is commodity,
cross-provider replanning is not, and an MCP surface is how that becomes
someone else's building block rather than our demo.

    python mcp_server.py          # stdio, replay-backed, no keys needed
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from demo_trip import TRIP, CEST, dt          # noqa: E402
from graph import propagate                   # noqa: E402
from plan import generate                     # noqa: E402
import aerodatabox, duffel, rail              # noqa: E402,E401

STATIONS = {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}


def _now(iso: str | None) -> datetime:
    return datetime.fromisoformat(iso) if iso else datetime(2026, 10, 12, 2, 38, tzinfo=CEST)


def impact_of(flight: str = "SQ346", now: str | None = None) -> dict:
    """What a delay breaks, what it costs, and which deadline falls first."""
    d = aerodatabox.disruption(flight, dt(11, 9, 0), "sq346")
    if not d:
        return {"disrupted": False}
    imp = propagate(TRIP, d, _now(now))
    when, node = imp.next_cutoff
    return {
        "disrupted": True,
        "delay_minutes": int(d.delay(TRIP).total_seconds() // 60),
        "do_nothing_eur": imp.do_nothing_cost,
        "recoverable_eur": imp.act_now_value,
        "next_cutoff": {"at": when.isoformat(), "booking": node.booking.title},
        "nodes": [{"id": n.id, "title": n.booking.title, "severity": n.severity.value,
                   "exposure_eur": n.exposure, "recoverable_eur": n.recoverable,
                   "why": n.reason} for n in imp.nodes],
    }


def recovery_plans(flight: str = "SQ346", now: str | None = None) -> dict:
    """Ranked recovery options, cheapest total damage first."""
    d = aerodatabox.disruption(flight, dt(11, 9, 0), "sq346")
    if not d:
        return {"disrupted": False}
    at = _now(now)
    offers = (duffel.offers("ZRH", "MXP", dt(12, 9, 0), after=d.new_end)
              + rail.offers("Zurich HB", "Milano Centrale", dt(12, 9, 0), STATIONS))
    return {"plans": [{
        "id": p.id, "name": p.name,
        "net_cash_eur": p.net_cash, "total_damage_eur": p.total_damage,
        "arrives": p.arrives_at.isoformat() if p.arrives_at else None,
        "actions": [{"lane": a.lane.value, "label": a.label,
                     "out_eur": a.cash_out, "in_eur": a.cash_in,
                     "deadline": a.deadline.isoformat() if a.deadline else None}
                    for a in p.actions],
    } for p in generate(TRIP, d, at, offers)]}


def main():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        sys.exit("pip install mcp   (this server is optional -- the engine, "
                 "the agent and the API do not depend on it)")
    server = FastMCP("downstream")
    server.tool()(impact_of)
    server.tool()(recovery_plans)
    server.run()


if __name__ == "__main__":
    main()
