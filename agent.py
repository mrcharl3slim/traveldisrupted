"""The LangGraph pipeline. Five nodes, one of which is allowed to think.

    detect -> propagate -> replan -> explain -> handoff

WHERE THE MODEL IS AND IS NOT. Four of these nodes are arithmetic wearing a
graph node's clothing, and that is deliberate. An agent that "reasons about"
whether EUR 48 is recoverable will eventually reason wrongly, in front of
judges, about the one number the pitch rests on. So detect, propagate, replan
and handoff are pure functions over typed data; the model reads fare prose at
ingestion (parse.py) and writes the plan's rationale here. Those are the two
jobs where language is the actual problem.

The graph shape still earns its keep: every node is separately observable,
which is what makes the OTEL traces on day 11 worth looking at, and what lets
the deadline monitor re-enter at ``propagate`` without re-running detection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

import aerodatabox
import duffel
import rail
from graph import propagate
from plan import Lane, generate

STATIONS = {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}


class State(TypedDict, total=False):
    trip: Any
    now: datetime
    flight: str
    flight_day: datetime
    booking_id: str
    search_day: datetime
    disruption: Any
    impact: Any
    plans: list
    chosen: Any
    rationale: str
    lanes: dict
    model: Any


def detect(s: State) -> State:
    return {"disruption": aerodatabox.disruption(
        s["flight"], s["flight_day"], s["booking_id"])}


def is_disrupted(s: State) -> str:
    """An on-time flight must cost nothing downstream. Most days are on time,
    and a product that replans them is a product nobody keeps installed."""
    return "propagate" if s.get("disruption") else END


def do_propagate(s: State) -> State:
    return {"impact": propagate(s["trip"], s["disruption"], s["now"])}


def replan(s: State) -> State:
    d, day = s["disruption"], s["search_day"]
    offers = (duffel.offers("ZRH", "MXP", day, after=d.new_end)
              + rail.offers("Zurich HB", "Milano Centrale", day, STATIONS))
    plans = generate(s["trip"], d, s["now"], offers)
    return {"plans": plans, "chosen": plans[0]}


def _template(s: State) -> str:
    """The fallback, and the thing the model has to beat.

    Deliberately good. If a templated sentence is indistinguishable from the
    model's, the model is not earning its latency and should be cut on day 12.
    """
    p, imp = s["chosen"], s["impact"]
    saved = imp.do_nothing_cost - p.total_damage
    tight = ""
    if p.tightest:
        bid, buf = p.tightest
        tight = (f" It holds the {s['trip'].by_id(bid).title.lower()} with "
                 f"{int(buf.total_seconds() // 60)} minutes to spare.")
    verb = (f"puts EUR {-p.net_cash:,.0f} back in your pocket" if p.net_cash < 0
            else f"costs EUR {p.net_cash:,.0f} tonight")
    return (f"{p.name} {verb} and leaves you EUR {saved:,.0f} better off than "
            f"doing nothing.{tight}")


def explain(s: State) -> State:
    model = s.get("model")
    fallback = _template(s)
    if model is None:
        return {"rationale": fallback}
    facts = {
        "plan": s["chosen"].name,
        "net_cash_eur": s["chosen"].net_cash,
        "total_damage_eur": s["chosen"].total_damage,
        "do_nothing_eur": s["impact"].do_nothing_cost,
        "actions": [a.label for a in s["chosen"].actions],
        # The label alone is not enough. An earlier version passed only
        # "Book EC 11:33, EUR 72" under a key called estimated_fares and
        # trusted the key name to carry the caveat -- which is exactly the kind
        # of thing a model skims past. The caveat now travels inside the string
        # the model reads.
        "estimated_fares": [f"{a.label} - THIS FARE IS AN ESTIMATE, not a quote"
                            for a in s["chosen"].actions if "ESTIMATE" in a.note],
    }
    try:
        reply = model.invoke([
            {"role": "system", "content":
             "Explain a travel recovery plan in at most two sentences to "
             "someone who has just landed late and is tired. Use only the "
             "numbers given. If estimated_fares is non-empty, say that fare is "
             "an estimate. No greetings, no exclamation marks."},
            {"role": "user", "content": str(facts)}])
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        return {"rationale": text.strip() or fallback}
    except Exception:                                   # noqa: BLE001
        # A model that is slow, rate-limited or misconfigured must cost the
        # traveller a nicer sentence, never the plan.
        return {"rationale": fallback}


def handoff(s: State) -> State:
    p = s["chosen"]
    return {"lanes": {lane.value: p.lane(lane) for lane in Lane}}


def build_graph():
    g = StateGraph(State)
    for name, fn in (("detect", detect), ("propagate", do_propagate),
                     ("replan", replan), ("explain", explain),
                     ("handoff", handoff)):
        g.add_node(name, fn)
    g.set_entry_point("detect")
    g.add_conditional_edges("detect", is_disrupted,
                            {"propagate": "propagate", END: END})
    g.add_edge("propagate", "replan")
    g.add_edge("replan", "explain")
    g.add_edge("explain", "handoff")
    g.add_edge("handoff", END)
    return g.compile()


GRAPH = build_graph()
