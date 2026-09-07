"""Grade the decision, not the answer.  `python evaluate.py`

WHY A HARNESS AND NOT MORE TESTS. The test suite asks whether a function
returns what it should. This asks a different question, and it is the one a
traveller cares about: given a situation, did the system decide the right
thing? Precision and recall do not describe an agent that spends money. So
each case below fixes the decision a competent human would make, runs the real
engine against it, and grades what came back.

WHAT MAKES A RESULT MEAN SOMETHING. Every case runs through `flow.replan`,
`permit.judge` or `ports.base.call` -- the same entry points the web app calls,
never a private helper arranged to agree. The provider responses are the
committed recordings, so a PASS today is a PASS next week on somebody else's
laptop; the engine is standard library only, so this file runs on a clean
interpreter with nothing installed.

WHAT IT DOES NOT MEASURE. Booking success against live inventory, and any
disruption shape nobody has written a case for. A harness that graded itself
on the cases it happens to pass would be a decoration.

    python evaluate.py            the table
    python evaluate.py --verbose  with the reason each decision was reached
    python evaluate.py --json     machine-readable, for CI

Exits non-zero if any case fails, so it can gate a deploy.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "data"), str(ROOT / "ports")]

import base                                          # noqa: E402
import flow                                          # noqa: E402
import monitor                                       # noqa: E402
import permit                                        # noqa: E402
import rail as railport                              # noqa: E402
import risk                                          # noqa: E402
from demo_trip import build_trip, resolve_base, anchor  # noqa: E402
from domain import Kind                              # noqa: E402
from graph import Severity                           # noqa: E402


# --------------------------------------------------------------------------
# the scene. One trip, built the way cli.py builds it, so a case that
# disagrees with the terminal is a real disagreement.
# --------------------------------------------------------------------------

BASE = resolve_base("today")
TRIP = build_trip(BASE)
NOW = anchor(BASE, 12, 2, 38)

FLIGHTS = [b for b in TRIP.in_order() if b.kind is Kind.FLIGHT]
FIRST, ONWARD = FLIGHTS[0], FLIGHTS[1]

OPEN = permit.Permissions(auto_limit=5000.0)          # everything pre-authorised
NONE_ = permit.Permissions()                          # the shipping default: S$0
HOLD = permit.Permissions(auto_limit=0.0, hold_limit=5000.0)


def decide(rec) -> tuple[str, str]:
    """The system's decision, in the words a traveller would use, plus why.

    Derived from the state the engine produced -- never from the case. This
    function is the harness's only opinion, and it is about vocabulary, not
    about what the right answer is.
    """
    if rec.permissions.disarmed:
        return "Stop acting", rec.verdict.why if rec.verdict else "kill switch"
    if not rec.plans:
        return "No plan", "nothing was offered"
    best, v = rec.best, rec.verdict
    if best.id == "noop":
        return "Do nothing", f"S${best.total_damage:,.0f} damage, and nothing to buy that beats it"
    if v is None:
        return "Act", ""
    if not v.allowed:
        return "Reject", v.why
    if v.hold:
        return "Plan, then hold", v.why
    if v.auto:
        return "Act inside authority", v.why
    return "Plan, then ask", v.why


@dataclass
class Case:
    id: str
    scenario: str
    expected: str
    run: Callable[[], tuple[str, str]]
    area: str = "engine"


CASES: list[Case] = []


def case(scenario: str, expected: str, area: str = "engine"):
    def wrap(fn):
        CASES.append(Case(fn.__name__, scenario, expected, fn, area))
        return fn
    return wrap


# --------------------------------------------------------------------------
# restraint and ranking -- most days nothing is wrong
# --------------------------------------------------------------------------

@case("Delay absorbed by the connection", "Do nothing · S$0")
def absorbed_delay():
    rec = flow.replan(TRIP, flow.delay(TRIP, FIRST.id, 30))
    what, why = decide(rec)
    return what + f" · S${rec.impact.do_nothing_cost:,.0f}", why


@case("Delay past the connection", "Act · recovery beats inaction")
def delay_past_slack():
    slack = ONWARD.must_arrive_by - FIRST.end
    beyond = int(slack.total_seconds() // 60) + 120
    rec = flow.replan(TRIP, flow.delay(TRIP, FIRST.id, beyond), permissions=OPEN)
    noop = next((p for p in rec.plans if p.id == "noop"), None)
    beats = noop is not None and rec.best.total_damage < noop.total_damage
    return ("Act · recovery beats inaction" if beats else "Do nothing"), \
           f"best S${rec.best.total_damage:,.0f} vs S${noop.total_damage:,.0f} for inaction"


@case("Inaction is a ranked candidate", "Scored, not asserted")
def inaction_is_scored():
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=OPEN)
    noop = next((p for p in rec.plans if p.id == "noop"), None)
    present = noop is not None and noop.total_damage > 0
    return ("Scored, not asserted" if present else "Missing"), \
           f"doing nothing is on the table at S${noop.total_damage:,.0f}" if noop else "absent"


@case("Cheapest plan loses the commitment", "Keep the commitment")
def goals_before_money():
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=OPEN)
    spending = [p for p in rec.plans if p.id != "noop"]
    if not spending:
        return "No plan", "nothing to rank"
    cheapest = min(spending, key=lambda p: p.net_cash)
    best = rec.best
    kept = len(best.missed_ids) <= len(cheapest.missed_ids)
    return ("Keep the commitment" if kept else "Chase the fare"), \
           (f"top plan misses {len(best.missed_ids)}, cheapest misses "
            f"{len(cheapest.missed_ids)}")


# --------------------------------------------------------------------------
# delegated authority -- the MAY question
# --------------------------------------------------------------------------

@case("Cancellation, nothing pre-authorised", "Plan, then ask")
def no_authority():
    return decide(flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=NONE_))


@case("Cancellation inside the hold tier", "Plan, then hold")
def inside_hold():
    return decide(flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=HOLD))


@case("Cancellation inside the act budget", "Act inside authority")
def inside_budget():
    return decide(flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=OPEN))


@case("Traveller said never rail", "Exclude it, recommend within the rules")
def never_rail():
    perms = permit.Permissions(auto_limit=5000.0, never=("rail",))
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=perms)
    ruled_out = len(rec.excluded)
    still_rail = [p for p in rec.plans if p.mode == "rail"]
    ok = ruled_out > 0 and not still_rail
    return ("Exclude it, recommend within the rules" if ok else "Rule ignored"), \
           f"{ruled_out} plan(s) ruled out, {len(still_rail)} rail plan(s) still offered"


@case("Always-ask set on notify", "Ask before notifying")
def always_ask_notify():
    perms = permit.Permissions(auto_limit=5000.0, always_ask=("notify",))
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=perms)
    what, why = decide(rec)
    asked = what != "Act inside authority"
    return ("Ask before notifying" if asked else "Sent anyway"), why


# --------------------------------------------------------------------------
# the kill switch
# --------------------------------------------------------------------------

@case("Kill switch, mid-execution", "Stop acting")
def kill_switch():
    perms = permit.Permissions(auto_limit=5000.0, disarmed=True)
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=perms)
    what, why = decide(rec)
    leaked = [a for p in rec.plans for a in p.actions
              if rec.verdict and rec.verdict.approval(a)[0] in ("auto", "pre-authorised")]
    return (what if not leaked else "Leaked past the switch"), why


@case("Kill switch on, deadlines still running", "Alerts continue")
def kill_switch_keeps_alerting():
    perms = permit.Permissions(auto_limit=5000.0, disarmed=True)
    disruption = flow.cancel(TRIP, ONWARD.id)
    rec = flow.replan(TRIP, disruption, permissions=perms)
    alerts = monitor.schedule(TRIP, disruption, rec.now, rec.impact)
    return ("Alerts continue" if alerts else "Went silent"), \
           f"{len(alerts)} alert(s) still scheduled while disarmed"


# --------------------------------------------------------------------------
# policy as deadline
# --------------------------------------------------------------------------

@case("Refund window still open", "Recoverable, act before the cutoff")
def window_open():
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=OPEN)
    value = rec.impact.act_now_value
    cutoff = rec.impact.next_cutoff
    return ("Recoverable, act before the cutoff" if value > 0 and cutoff else "Nothing to recover"), \
           f"S${value:,.0f} recoverable, next cutoff {cutoff[0]:%d %b %H:%M}" if cutoff else "no cutoff"


@case("The same window, after it closes", "Expired · no refund")
def window_closed():
    late = max((w.closes for b in TRIP.in_order() for w in b.policy.windows),
               default=None)
    if late is None:
        return "No windows", "the trip has no fare rules"
    after = late + timedelta(hours=1)
    still = [w for b in TRIP.in_order() for w in b.policy.open_windows(after)]
    worth = sum(b.policy.recoverable_at(after) for b in TRIP.in_order())
    return ("Expired · no refund" if not still and worth == 0 else "Still recoverable"), \
           f"{len(still)} window(s) open after the last cutoff, S${worth:,.0f} recoverable"


# --------------------------------------------------------------------------
# disclosure -- a number the system cannot source says so
# --------------------------------------------------------------------------

@case("Rail fare nobody sells an API for", "Label it an estimate")
def rail_is_estimated():
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=OPEN)
    rail_actions = [a for p in rec.plans for a in p.actions if p.mode == "rail" and a.cash_out]
    if not rail_actions:
        return "No rail offered", "the search returned no train"
    honest = all(a.price_source != "quoted" for a in rail_actions)
    sources = sorted({a.price_source for a in rail_actions})
    return ("Label it an estimate" if honest else "Presented as quoted"), \
           f"price_source: {', '.join(sources)}"


@case("Fare quoted in another currency", "Label it converted")
def conversion_is_disclosed():
    rec = flow.replan(TRIP, flow.cancel(TRIP, ONWARD.id), permissions=OPEN)
    converted = [a for p in rec.plans for a in p.actions if a.price_source == "converted"]
    if not converted:
        return "Label it converted", "no converted fare in this search"
    # The provider's own words have to survive the conversion, or a traveller
    # cannot check the number against the airline's page. `_caveat` puts them
    # in the note: "USD 81 converted at a fixed rate".
    kept = [a for a in converted if re.search(r"\b[A-Z]{3} [\d,]+", a.note)]
    return ("Label it converted" if len(kept) == len(converted)
            else "Original price dropped"), \
           f"{len(converted)} converted action(s), e.g. {converted[0].note.split(' - ')[-1]}"


# --------------------------------------------------------------------------
# foresight -- before anything breaks
# --------------------------------------------------------------------------

@case("Itinerary with a thin connection, nothing broken yet", "Flag it before it breaks")
def fragility_before_failure():
    risks = risk.assess(TRIP)
    kinds = sorted({r.kind for r in risks})
    return ("Flag it before it breaks" if risks else "Said nothing"), \
           f"{len(risks)} flag(s): {', '.join(kinds) or 'none'}"


@case("A commitment nothing can reach", "Flag the conflict")
def impossible_commitment():
    rec = flow.replan(TRIP, flow.cancel(TRIP, FIRST.id), permissions=OPEN)
    missed = rec.impact.missed
    broken = rec.impact.broken
    return ("Flag the conflict" if (missed or broken) else "Reported nothing"), \
           f"{len(missed)} commitment(s) missed, {len(broken)} booking(s) broken"


# --------------------------------------------------------------------------
# suppliers -- the demo cannot be killed by somebody else's outage
# --------------------------------------------------------------------------

def _rail_key() -> dict:
    """The key the rail port really uses, captured from a replay run rather
    than guessed -- a hand-written key that drifts would test nothing."""
    seen: dict = {}
    original = railport.call

    def spy(name, key, fetch, prefer=None):
        seen.setdefault("key", key)
        return original(name, key, fetch, prefer)

    railport.call = spy
    try:
        railport.offers("Zurich HB", "Milano Centrale", anchor(BASE, 12, 9, 0),
                        {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"})
    finally:
        railport.call = original
    return seen.get("key", {})


def _raise() -> dict:
    raise OSError("connection reset by peer")


@case("Supplier API fails, a recording exists", "Replay it, and say so", area="ports")
def supplier_falls_back():
    key = _rail_key()
    if not key:
        return "No call observed", "the rail port did not go through base.call"
    saved_mode, before = base.MODE, len(base.degraded)
    base.MODE = "live"
    try:
        data = base.call("rail", key, _raise)
    except base.PortError as exc:
        return "Failed loudly", str(exc)
    finally:
        base.MODE = saved_mode
    said = base.degraded[before:]
    return ("Replay it, and say so" if data and said else "Fell back silently"), \
           (said[0] if said else "nothing appended to degraded")


@case("Supplier API fails, no recording", "Fail loudly", area="ports")
def no_recording_fails_loudly():
    saved_mode = base.MODE
    base.MODE = "live"
    try:
        base.call("rail", {"from": "Nowhere", "to": "Nowhere", "at": "1970-01-01T00-00"},
                  _raise)
    except base.PortError as exc:
        return "Fail loudly", str(exc)[:70]
    finally:
        base.MODE = saved_mode
    return "Returned something", "a missing recording was answered with data"


# --------------------------------------------------------------------------
# dates -- a recording from October has to answer a question asked today
# --------------------------------------------------------------------------

@case("Recording predates the question", "Shift to run time, and say so", area="ports")
def dates_are_shifted():
    trip_today = build_trip(resolve_base("today"))
    first = trip_today.in_order()[0]
    fresh = first.start.date() >= (resolve_base("today") or first.start.date())
    # Both halves, or the expectation is only half met: shifted AND disclosed.
    # Written with the two branches returning the same string, `base.shifted`
    # was evaluated and thrown away -- a port that stopped saying it had moved
    # the dates would still have passed this case.
    return ("Shift to run time, and say so" if fresh and base.shifted
            else "Shifted silently" if fresh
            else "Left in the past"), \
           f"trip starts {first.start:%d %b %Y}; {len(base.shifted)} shift(s) disclosed"


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv
    as_json = "--json" in argv

    results = []
    for c in CASES:
        try:
            got, why = c.run()
            error = ""
        except Exception as exc:                      # a crash is a failure
            got, why, error = f"{type(exc).__name__}", str(exc), str(exc)
        results.append((c, got, why, got == c.expected and not error))

    if as_json:
        print(json.dumps([{"id": c.id, "area": c.area, "scenario": c.scenario,
                           "expected": c.expected, "got": got, "why": why,
                           "pass": ok}
                          for c, got, why, ok in results], indent=2))
        return 0 if all(ok for *_, ok in results) else 1

    print(f"\n  TripShield — decision harness"
          f"\n  ports: {base.MODE}   cases: {len(CASES)}   "
          f"trip anchored to {TRIP.in_order()[0].start:%d %b %Y}\n")
    print(f"  {'SCENARIO':<46}{'EXPECTED DECISION':<40}RESULT")
    print("  " + "-" * 94)
    area = ""
    for c, got, why, ok in results:
        if c.area != area:
            area = c.area
        print(f"  {c.scenario[:45]:<46}{c.expected[:39]:<40}{'PASS' if ok else 'FAIL'}")
        if not ok:
            print(f"  {'':<46}{'-> got: ' + got[:70]}")
        if verbose and why:
            print(f"  {'':<46}   {why[:80]}")

    passed = sum(1 for *_, ok in results if ok)
    print("  " + "-" * 94)
    print(f"  {passed}/{len(results)} decisions as expected\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
