"""The conversation, as a graph.

    read -> classify -> [confirmation] ingest -> done
                     -> [request]      fill -> ask        (stop, wait for a human)
                                            -> route -> search -> offer

WHY A GRAPH AND NOT A PROMPT LOOP. Every node here is a pure function of the
state except one, and the one is fenced. `read` parses, `fill` decides what is
missing, `route` picks the providers, `search` calls them, `offer` ranks what
came back. A single prompt could do all of that and would occasionally book
Bangkok because the traveller mentioned it in passing. Separating them means
each step is separately testable, separately traceable, and separately
falsifiable -- and it means the whole conversation still runs with
LLM_PROVIDER=none, just with less patience for unusual phrasing.

WHY IT RESTARTS EVERY TURN. HTTP has no memory and neither does this. The state
is serialised to the store between turns and the graph is re-entered from the
top, which sounds wasteful and is the reason a refresh, a second tab or a
process restart cannot lose a half-finished booking. Parsing is cheap;
re-asking a question the traveller already answered is not.

WHAT IT REFUSES TO DO. It never fills a slot to avoid asking. An agent that
guesses "probably no hotel" to keep the conversation moving has made a booking
decision on the traveller's behalf, and the entire premise of this product is
that unstated assumptions are what cost people money later.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

import places
import request as request_mod
from builder import MIN_CONNECTION, onward_after

#: How many options to put in front of a person. More than this is a search
#: results page, and a search results page is the thing they were trying to
#: avoid by typing a sentence.
SHOW = 5


class State(TypedDict, total=False):
    text: str
    today: date
    model: Any
    req: request_mod.Request
    kind: str
    asks: list
    ports: list[str]
    flights: list
    returns: list
    stays: list
    note: str
    detail: str
    missing: list[str]
    reply: str


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------


def _set(value) -> bool:
    """Has this slot been settled?

    False is a settled answer for `one_way` and `hotel` and an empty one for a
    string, which is why this asks about None and emptiness rather than
    truthiness. `hotel=False` means "no room, thank you" and must survive the
    next sentence.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        # Checked before the numeric test, because `False == 0` is True in
        # Python and `hotel=False` is a settled answer -- "no room, thank you"
        # -- that has to survive the next sentence. Without this line the agent
        # asks about a hotel the traveller has already declined.
        return True
    return value != "" and value != 0


def read(s: State) -> State:
    """Text -> a Request, deterministically, then the model on the blanks."""
    base = s.get("req") or request_mod.Request()
    # A date already on the card gives a bare "the 19th" its month.
    fresh = request_mod.parse(s.get("text", ""), s.get("today"), base.depart)

    # A follow-up sentence adds to what is already known rather than replacing
    # it. "actually make it the 3rd" must not wipe the destination.
    #
    # FIELD-AGNOSTIC, for the same reason `shift_trip` is. The first version
    # named every field, and the first field added after it -- how many nights
    # the room is for -- was silently dropped here: parsed correctly, carried
    # nowhere, and the agent asked a question the traveller had already
    # answered in their opening sentence. Naming fields means the next one
    # added is forgotten too, and the failure is not a crash but a question
    # that will not go away.
    # Additive while collecting, OVERWRITING while correcting.
    #
    # "actually make it the 3rd" must not wipe the destination -- so during
    # collection a settled slot wins over anything a later sentence says. But
    # once everything is settled and the traveller is looking at "have I got
    # this right?", the only reason to type is to change something, and an
    # additive merge would silently ignore them. Answering a confirmation with
    # a correction that changes nothing is worse than not offering to confirm.
    correcting = (not base.confirmed
                  and [a.field for a in base.gaps() if not a.optional] == ["confirm"])
    merged = replace(base, **{
        f.name: (getattr(fresh, f.name)
                 if (correcting and _set(getattr(fresh, f.name)))
                 else getattr(base, f.name) if _set(getattr(base, f.name))
                 else getattr(fresh, f.name))
        for f in fields(base) if f.name not in ("raw", "filled", "travellers",
                                                "confirmed")
    })
    merged = replace(merged,
                     travellers=max(base.travellers, fresh.travellers),
                     raw=s.get("text", "") or base.raw)
    return {"req": request_mod.enrich(merged, s.get("model"), s.get("today"))}


def classify(s: State) -> State:
    """A booking request, or a confirmation someone forwarded in.

    Told apart by shape, not by asking a model: a confirmation carries a
    booking reference, a fare rule, or the word "confirmed" next to a price. A
    request is a sentence about the future. Getting this wrong in the safe
    direction -- treating a confirmation as a request -- asks a question, which
    a human can correct in one word.
    """
    text = s.get("text", "")
    low = text.lower()

    # An appointment is something to BE at; a request is something to buy.
    # Told apart by the verb, not by a model: "meeting with", "call with",
    # "dinner with" are arrangements, and none of them mention a fare.
    appointment = re.search(
        r"\b(meeting|appointment|call|conference|workshop|interview|"
        r"standup|stand-up|review|catch[- ]?up|coffee|drinks|dinner|lunch|"
        r"breakfast|site visit|viewing|class|lecture|ceremony|wedding|"
        r"handover|demo)\b", low)
    buying = re.search(r"\b(flight|fly|book me|hotel|room|one way|return|"
                       r"cheapest|airfare)\b", low)

    marks = sum(1 for m in (
        "booking reference", "confirmation number", "e-ticket", "pnr",
        "your booking", "booking confirmed", "check-in", "check in",
        "non-refundable", "fare rules", "total paid", "order number",
    ) if m in low)
    long_enough = len(text) > 220 and text.count("\n") >= 3

    if marks >= 2 and long_enough:
        return {"kind": "confirmation"}
    # "dinner in Milan" while booking a trip is part of the trip, not a
    # separate arrangement -- so buying language wins when both are present.
    if appointment and not buying:
        return {"kind": "appointment"}
    return {"kind": "book"}


def branch(s: State) -> str:
    return "ingest" if s.get("kind") == "confirmation" else "fill"


def fill(s: State) -> State:
    return {"asks": s["req"].gaps()}


def needs_answers(s: State) -> str:
    """Optional gaps never stop a search.

    The distinction matters more than it looks: "anywhere in particular to
    stay?" is worth offering and not worth blocking on, and an agent that
    treats every unanswered question as a blocker is an interrogation.
    """
    return "ask" if [a for a in s["asks"] if not a.optional] else "route"


def ask(s: State) -> State:
    blocking = [a for a in s["asks"] if not a.optional]
    first = blocking[0]
    return {"reply": first.question}


def route(s: State) -> State:
    """Which providers this request actually needs.

    Named in the state so the page can show it. "Searching Duffel for flights
    and LiteAPI for stays" is the moment the thing stops looking like a chatbot
    and starts looking like something with its hands on real systems -- and it
    is also the honest place to say that rail has no fares and hotels are not
    being paid for.
    """
    req = s["req"]
    ports = ["duffel"]
    if not req.one_way and req.ret:
        ports.append("duffel:return")
    if req.hotel:
        ports.append("liteapi")
    return {"ports": ports}


def search(s: State) -> State:
    """Call them. A provider that comes back empty is a fact, not an error."""
    import flow
    from base import PortError

    req = s["req"]
    out: State = {"flights": [], "returns": [], "stays": [],
                  "note": "", "detail": "", "missing": []}
    plain: list[str] = []
    raw: list[str] = []

    def attempt(what: str, human: str, call):
        """Run one provider call. An empty leg is a fact, not an exception.

        Two messages come out of every failure. The port's own words name a
        fixture path and a shell command -- exactly right for whoever is
        running this, and exactly wrong in a chat window, where it reads as the
        software falling over rather than as inventory it has not been given.
        The traveller gets the sentence; the detail stays available.
        """
        try:
            return call()
        except PortError as exc:
            raw.append(str(exc))
            plain.append(human)
            out["missing"].append(what)
            return []

    # Concurrently, because these three do not depend on each other and the
    # traveller is watching a spinner. Run one after another against live
    # providers this is four HTTP round trips end to end -- outbound, return,
    # hotel catalogue, hotel rates -- and a slow one anywhere in the chain
    # pushes the whole request past the hosting gateway's patience, which then
    # answers with its own error page instead of ours.
    #
    # Threads rather than async: the ports are deliberately plain urllib, so
    # that `cli.py` runs on a clean interpreter and the arithmetic never
    # depends on an event loop. A pool is the cheap way to overlap them without
    # colouring the whole call chain.
    jobs = [("outbound",
             f"no inventory recorded for {places.label(req.origin)} to "
             f"{places.label(req.destination)} on {req.depart:%d %b}",
             lambda: flow.search_flights(req.origin, req.destination,
                                         _noon(req.depart, _zone(req.origin))))]

    if req.ret and not req.one_way:
        jobs.append(("return",
                     f"no inventory recorded for the return, "
                     f"{places.label(req.destination)} to "
                     f"{places.label(req.origin)} on {req.ret:%d %b}",
                     lambda: flow.search_flights(
                         req.destination, req.origin,
                         _noon(req.ret, _zone(req.destination)))))

    if req.hotel:
        place = places.by_code(req.destination)
        # The whole trip, from `Request.check_out` -- a return date when there
        # is one, and otherwise the nights the traveller was asked for.
        checkout = req.check_out
        jobs.append(("stays",
                     f"no stays recorded in {place.hotel_city} for those nights",
                     lambda: flow.search_hotels(
                         place.hotel_city, place.country,
                         _noon(req.check_in, _zone(req.destination)),
                         _noon(checkout, _zone(req.destination)),
                         code=req.destination)))

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(attempt, what, human, call): what
                   for what, human, call in jobs}
        for future in futures:
            out[{"outbound": "flights", "return": "returns",
                 "stays": "stays"}[futures[future]]] = future.result()

    out["note"] = " · ".join(plain)
    out["detail"] = " · ".join(raw)
    return out


def offer(s: State) -> State:
    """Rank by what the traveller said mattered, and say what was ranked."""
    req = s["req"]
    return {
        "flights": rank(s.get("flights") or [], req.preference)[:SHOW],
        "returns": rank(s.get("returns") or [], req.preference)[:SHOW],
        "stays": (s.get("stays") or [])[:SHOW],
        "reply": _summarise(s),
    }


# --------------------------------------------------------------------------
# ranking -- the preference, honoured
# --------------------------------------------------------------------------


def stops(offer_) -> int:
    """Segments minus one, read off the label the port already built."""
    label = getattr(offer_, "label", "")
    if " via " in label:
        return 1
    marker = label.rsplit(", ", 1)[-1]
    if marker.endswith("stops"):
        try:
            return int(marker.split()[0])
        except ValueError:
            return 0
    return 0


def minutes(offer_) -> int:
    return int((offer_.arrive - offer_.depart).total_seconds() // 60)


def rank(offers: list, preference: str) -> list:
    """One preference, three orderings, no thumb on any of them.

    "direct" is a preference and not a filter: a traveller who wants a direct
    flight and is shown nothing because none exists has been failed by the
    software, not by the airlines. Direct ones come first and the rest follow,
    labelled.
    """
    if preference == "fastest":
        return sorted(offers, key=lambda o: (minutes(o), o.price))
    if preference == "direct":
        return sorted(offers, key=lambda o: (stops(o), o.price, minutes(o)))
    return sorted(offers, key=lambda o: (o.price, minutes(o)))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _zone(code: str):
    from zoneinfo import ZoneInfo

    place = places.by_code(code)
    try:
        return ZoneInfo(place.zone) if place else ZoneInfo("UTC")
    except Exception:                                   # noqa: BLE001
        return ZoneInfo("UTC")


def _noon(day: date, zone) -> datetime:
    """Midday, so no offset error can move a search to the day before."""
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=zone)


def _summarise(s: State) -> str:
    req = s["req"]
    counts = []
    if s.get("flights"):
        counts.append(f"{len(s['flights'])} outbound")
    if s.get("returns"):
        counts.append(f"{len(s['returns'])} return")
    if s.get("stays"):
        counts.append(f"{len(s['stays'])} stays")
    if not counts:
        if s.get("missing"):
            return ("I have nothing recorded for that route yet — in replay "
                    "mode I can only offer inventory that has been captured. "
                    "Try Zurich to Milan on 18 September, or record this one.")
        return ("Nothing came back for that. "
                + (s.get("note") or "Try a different date or airport."))
    order = {"cheapest": "cheapest first", "fastest": "shortest first",
             "direct": "direct first"}.get(req.preference, "cheapest first")
    return f"{', '.join(counts)} — {order}."


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------


def build():
    g = StateGraph(State)
    g.add_node("read", read)
    g.add_node("classify", classify)
    g.add_node("fill", fill)
    g.add_node("ask", ask)
    g.add_node("route", route)
    g.add_node("search", search)
    g.add_node("offer", offer)
    g.add_node("ingest", lambda s: {"reply": "That looks like a booking you "
                                             "already have — I'll read it in."})

    g.set_entry_point("read")
    g.add_edge("read", "classify")
    g.add_conditional_edges("classify", branch, {"ingest": "ingest", "fill": "fill"})
    g.add_conditional_edges("fill", needs_answers, {"ask": "ask", "route": "route"})
    g.add_edge("route", "search")
    g.add_edge("search", "offer")
    g.add_edge("offer", END)
    g.add_edge("ask", END)
    g.add_edge("ingest", END)
    return g.compile()


_GRAPH = None


def turn(text: str, req: request_mod.Request | None = None,
         model=None, today: date | None = None) -> State:
    """One exchange. Re-entered from the top with the state so far."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH.invoke({"text": text, "req": req, "model": model,
                          "today": today or date.today()})
