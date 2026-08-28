"""Print the derived impact map. `python cli.py`

Exists so the claim "computed, not typed" can be checked in ten seconds by
anyone, including a judge leaning over a laptop.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

from demo_trip import CEST, DEFAULT_BASE, anchor, build_trip, resolve_base  # noqa: E402
from domain import Disruption                   # noqa: E402
from graph import propagate                     # noqa: E402
import aerodatabox, duffel, rail                 # noqa: E402,E401
from base import MODE, degraded, shifted         # noqa: E402
from plan import Lane, generate                 # noqa: E402
from offers import build_offers                  # noqa: E402


def _base(argv: list[str]) -> date | None:
    """--base today | +N | YYYY-MM-DD | written.  Defaults to today."""
    if "--base" not in argv:
        return resolve_base("today")
    index = argv.index("--base")
    return resolve_base(argv[index + 1] if len(argv) > index + 1 else "today")


BASE = _base(sys.argv)
TRIP = build_trip(BASE)

# 02:38 on the second night: after the delay is known, and 3 h 22 m before the
# transfer's free-change window closes. Derived from the trip so it travels with it.
NOW = anchor(BASE, 12, 2, 38)

# Detection and options now come through the ports. In replay that is a
# recorded response; in live it is the provider. Nothing below knows which.
D = aerodatabox.disruption("SQ346", anchor(BASE, 11, 9, 0), "sq346")
OFFERS = (duffel.offers("ZRH", "MXP", anchor(BASE, 12, 9, 0), after=D.new_end)
          + rail.offers("Zurich HB", "Milano Centrale", anchor(BASE, 12, 9, 0),
                        {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}))
print(f"\n  ports: {MODE}"
      + (f"  DEGRADED: {'; '.join(degraded)}" if degraded else "")
      + (f"\n  dates:  anchored to {TRIP.in_order()[0].start:%d %b %Y}"
         f"  ({'; '.join(shifted)})" if shifted else ""))

imp = propagate(TRIP, D, NOW)

print(f"\n  SIGNAL   {TRIP.by_id(D.booking_id).title}")
print(f"           delayed {D.delay(TRIP)}, arrives {D.new_end:%H:%M} "
      f"(was {TRIP.by_id(D.booking_id).end:%H:%M}), confidence {D.confidence:.0%}\n")

print(f"  {'BOOKING':<34}{'SEVERITY':<11}{'EXPOSED':>9}{'RECOVER':>9}   CUTOFF")
print("  " + "-" * 78)
for n in imp.nodes:
    cut = f"{n.cutoff:%d %b %H:%M}" if n.cutoff else ""
    exp = f"{n.exposure:,.0f}" if n.exposure else ""
    rec = f"{n.recoverable:,.0f}" if n.recoverable else ""
    print(f"  {n.booking.title[:33]:<34}{n.severity.value:<11}{exp:>9}{rec:>9}   {cut}")

when, node = imp.next_cutoff
print("\n  " + "-" * 78)
print(f"  DO NOTHING       EUR {imp.do_nothing_cost:,.0f} lost across "
      f"{len(imp.broken)} bookings")
print(f"  ACT IN TIME      EUR {imp.act_now_value:,.0f} recoverable")
print(f"  NEXT DEADLINE    {when:%H:%M} - {node.booking.title} "
      f"(in {when - NOW})\n")

plans = generate(TRIP, D, NOW, OFFERS)
print(f"  {'PLAN':<44}{'TONIGHT':>10}{'DAMAGE':>9}   ARRIVES")
print("  " + "-" * 78)
for p in plans:
    cash = f"{p.net_cash:+,.0f}" if p.net_cash else "0"
    arr = f"{p.arrives_where} {p.arrives_at:%H:%M}" if p.arrives_at else "-"
    est = " *" if any(a.price_source == "estimate" for a in p.actions) else ""
    print(f"  {p.name[:43]:<44}{cash:>10}{p.total_damage:>9,.0f}   {arr}{est}")
print("\n  * fare is an estimate - no reachable API quotes Swiss rail prices")

best = plans[0]
print(f"\n  BEST: {best.name}")
if best.tightest:
    bid, buf = best.tightest
    print(f"  Tightest connection: {TRIP.by_id(bid).title} with "
          f"{int(buf.total_seconds() // 60)} min to spare")
for lane, title in ((Lane.AUTO, "Downstream handles"),
                    (Lane.TAP, "One tap, you authorise"),
                    (Lane.CALL, "You'll have to call")):
    acts = best.lane(lane)
    if not acts:
        continue
    print(f"\n  {title}")
    for a in acts:
        money = ""
        if a.cash_out:
            money = f"  -EUR {a.cash_out:,.0f}"
        elif a.cash_in:
            money = f"  +EUR {a.cash_in:,.0f}"
        by = f"  (by {a.deadline:%H:%M})" if a.deadline else ""
        print(f"    - {a.label}{money}{by}")
print()
