"""What to search for, when the answer is not one search.

`plan.recovery_gap` already derives the query the engine should run: where the
traveller stands, where they are contractually due, and by when. That is one
query, and `agent.replan` turns it into exactly two hard-coded searches --
Duffel between the airports, SBB between the stations. It works because the
scripted scenario is a flight and a train between two cities that have both.

The searches nobody wrote down are the interesting ones. Fly into the other
airport and take the ground link. Leave from the station in the city you are
already stuck in rather than the one nearest the airport. Give up on the
16:00 slot and search tomorrow morning instead. A recovery worth having is
often the one the author of `replan` did not think to enumerate, and
enumeration is precisely the job a model is better at than a table.

WHAT THE MODEL MAY AND MAY NOT SAY. It proposes a Probe: a mode, two places
and a window. It never proposes an Offer, a price, a flight number or a
departure time, because every one of those is something a provider knows and
a model would be guessing. The ports resolve a probe into real offers, or
refuse it with a reason; `plan.build` prices whatever survives. Model
proposes, arithmetic disposes -- the same split domain.py draws between what
was parsed and what was computed.

TIMES ARE OFFSETS, NOT DATES. parse.py learned this the hard way: a model
allowed to write an absolute timestamp will occasionally write the right time
on the wrong day, and nothing downstream can tell. So a probe's window arrives
as minutes relative to the gap, and Python resolves it against the gap's own
clock. A model that is wrong about "180 minutes later" is visibly wrong; a
model that is wrong about the date is invisibly wrong.

REFUSAL IS THE FEEDBACK CHANNEL. A rejected probe returns a typed Reason and a
hint written for the next prompt. That is what turns a proposer into a loop
with somewhere to go: "MILANO_X is not a place I know" and "no departures in
that window" ask for different second attempts, and a boolean asks for none.
The loop is bounded by Budget rather than by the model deciding it is done --
a critic that is never satisfied is a bill, not a design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

import places

#: What a probe may ask for. Not an open vocabulary: each mode names a port
#: that exists, and a mode nobody implemented is a search that silently
#: returns nothing rather than a search that says it cannot be run.
MODES = ("flight", "rail")

#: How far either edge of the window may be pushed from the gap the engine
#: derived. A model asked for "a wider window" with no bound will eventually
#: propose next week, and next week is not a recovery from tonight.
MAX_SHIFT = timedelta(hours=36)

#: How far past the gap's deadline a search still looks, by default.
#:
#: The deadline is NOT a filter here, and getting that wrong would delete a
#: shipped plan. `recovery_gap` aims at the first booking the traveller can no
#: longer attend -- for the scripted disruption, a 16:00 museum slot. Cutting
#: the search there loses LX 1626, which lands at 16:35, gives that slot up and
#: rescues the rest of the trip: Plan A, ranked and shown to judges today.
#: Whether missing the target is worth it is `plan.build`'s arithmetic. The
#: searcher's job is candidates.
HORIZON = timedelta(hours=12)

#: Two searches this close together are the same search.
NEAR = timedelta(minutes=20)


class Reason(str, Enum):
    """Why a probe produced nothing. The string is shown to a human."""

    MALFORMED = "the proposal did not parse"
    UNKNOWN_PLACE = "not a place this system knows"
    NO_STATION = "no station worth naming there"
    SAME_PLACE = "origin and destination are the same"
    EMPTY_WINDOW = "the window ends before it starts"
    OUT_OF_RANGE = "the window is too far from the disruption"
    DUPLICATE = "already searched"
    PORT_UNAVAILABLE = "the provider could not be reached and has no recording"
    NO_OFFERS = "the provider returned nothing in that window"
    OUT_OF_BUDGET = "the search budget for this run is spent"


#: Reasons where a *different* probe is worth trying. The rest are dead ends:
#: proposing MILANO_X again more slowly does not make it a station.
WORTH_ANOTHER = frozenset({Reason.NO_OFFERS, Reason.EMPTY_WINDOW,
                           Reason.OUT_OF_RANGE, Reason.PORT_UNAVAILABLE})


@dataclass(frozen=True)
class Probe:
    """One search, fully specified and not yet run."""

    mode: str
    origin: str
    destination: str
    not_before: datetime
    by: datetime
    why: str = ""

    @property
    def key(self) -> str:
        """Identity for de-duplication. The window is rounded to the quarter
        hour: two probes eleven minutes apart are the same search, and paying
        a provider twice to learn that is how a budget disappears."""
        floor = self.not_before.replace(
            minute=self.not_before.minute // 15 * 15, second=0, microsecond=0)
        return f"{self.mode}:{self.origin}->{self.destination}@{floor:%Y%m%dT%H%M}"

    def __str__(self) -> str:
        end = (f"{self.by:%H:%M}" if self.by.date() == self.not_before.date()
               else f"{self.by:%d %b %H:%M}")
        return (f"{self.mode} {self.origin} to {self.destination}, "
                f"{self.not_before:%d %b %H:%M}-{end}")


@dataclass(frozen=True)
class Rejected:
    probe: Probe | None
    reason: Reason
    detail: str = ""

    @property
    def hint(self) -> str:
        """The sentence handed back to the next prompt."""
        head = str(self.probe) if self.probe else "that proposal"
        return f"{head}: {self.reason.value}{'. ' + self.detail if self.detail else ''}"


@dataclass(frozen=True)
class Resolved:
    probe: Probe
    offers: list


@dataclass
class Budget:
    """The iteration cap, held in state rather than in the model's judgement.

    Three separate counters because they run out for different reasons and a
    single number hides which. Rounds bound the conversation, probes bound how
    wide one round may fan out, and calls bound what the providers are asked --
    AeroDataBox allows 600 units a month and an unbounded proposer would spend
    a demo's worth in one enthusiastic afternoon.

    `probes` counts what the MODEL proposed. The seeded floor is deliberately
    free: it is what the product searches today, and a budget that can refuse
    it is a budget that can turn the model being unavailable into the traveller
    being told nothing. `calls` still bounds the floor, because that is the
    counter that protects the provider quota.
    """

    rounds: int = 3
    probes: int = 8
    calls: int = 10
    spent: dict = field(default_factory=lambda: {"rounds": 0, "probes": 0, "calls": 0})

    def take(self, what: str) -> bool:
        if self.spent[what] >= getattr(self, what):
            return False
        self.spent[what] += 1
        return True

    @property
    def exhausted(self) -> bool:
        return all(self.spent[k] >= getattr(self, k) for k in self.spent)


# ----------------------------------------------------------------- vocabulary

def vocabulary() -> dict[str, list[str]]:
    """The codes a probe may name, per mode, straight from places.py.

    Derived rather than listed. A second hard-coded table is a second thing to
    forget to update, and the failure it produces -- a model proposing a
    station this build cannot resolve -- looks exactly like a model being
    stupid.
    """
    return {
        "flight": sorted(p.code for p in places.PLACES),
        "rail": sorted(p.station_code for p in places.PLACES if p.station_code),
    }


def terminals(mode: str, code: str) -> list[tuple[str, timedelta]]:
    """Places a search of this mode can name, and the ground time to ``code``.

    THE ASSUMPTION THIS REPLACES. `agent.replan` searches ZRH to MXP and Zurich
    HB to Milano Centrale because the scenario says so -- and the derived gap
    does not agree: it aims at SMG, Santa Maria delle Grazie, which is a church
    with a fresco and no departure board. Neither an airline nor a timetable
    has ever heard of it.

    A gap's endpoint is where the traveller must BE. What a provider can sell
    is a terminal near it. The link between the two is already in
    `domain.TRANSIT`, which exists precisely because a station is not its
    airport -- so the terminals are derived from it rather than listed again
    here, and the two searches `replan` hard-codes fall out as the answer for
    this gap instead of being the premise.

    Nearest first: a fifty-minute ground link is a worse way to reach the same
    place than a twenty-minute one, and a bounded proposer should spend its
    budget in that order.
    """
    from domain import TRANSIT, transit

    out: list[tuple[str, timedelta]] = []
    direct, bad = _resolve_place(mode, code)
    if not bad and direct:
        # Resolving a place for rail turns an airport into its station, and
        # those are not the same building: Zurich airport to Zurich HB is
        # twelve minutes, and a traveller who lands at 10:25 is not standing on
        # that platform at 10:25. `transit` returning None is the answer from
        # domain.py -- no ground route, so not a terminal for this place, which
        # is why the pair is dropped rather than given a zero.
        ground = transit(code, direct)
        if ground is not None:
            out.append((direct, ground))

    for a, b in TRANSIT:
        for near, far in ((a, b), (b, a)):
            if far != code:
                continue
            resolved, bad = _resolve_place(mode, near)
            if bad or not resolved or any(resolved == c for c, _ in out):
                continue
            ground = timedelta(minutes=TRANSIT[(a, b)])
            out.append((resolved, ground))
    return sorted(out, key=lambda pair: pair[1])


def _resolve_place(mode: str, code: str) -> tuple[str | None, Reason | None]:
    place = places.by_code(code)
    if place is None:
        return None, Reason.UNKNOWN_PLACE
    if mode == "rail":
        return (place.station_code, None) if place.station_code else (None, Reason.NO_STATION)
    return place.code, None


def check(mode: str, origin: str, destination: str, not_before: datetime,
          by: datetime, gap_start: datetime, why: str = "") -> Probe | Rejected:
    """A proposal, validated into a Probe or refused with a reason.

    Every refusal here is one the model can act on, which is why the checks are
    separate rather than one boolean. Placing this between the model and the
    ports also means a malformed proposal costs nothing: no provider is called,
    no budget is spent, and the reason goes back into the next prompt.
    """
    if mode not in MODES:
        return Rejected(None, Reason.MALFORMED, f"mode {mode!r} is not one of {MODES}")

    o, bad = _resolve_place(mode, origin)
    if bad:
        return Rejected(None, bad, f"origin {origin!r}")
    d, bad = _resolve_place(mode, destination)
    if bad:
        return Rejected(None, bad, f"destination {destination!r}")
    if o == d:
        return Rejected(None, Reason.SAME_PLACE, o or "")

    probe = Probe(mode, o, d, not_before, by, why)
    if by <= not_before:
        return Rejected(probe, Reason.EMPTY_WINDOW)
    if abs(not_before - gap_start) > MAX_SHIFT or abs(by - gap_start) > MAX_SHIFT:
        return Rejected(probe, Reason.OUT_OF_RANGE,
                        f"more than {MAX_SHIFT.total_seconds() // 3600:.0f} hours from the disruption")
    return probe


# ----------------------------------------------------------------- proposing

def seed(gap) -> list[Probe]:
    """The searches the gap itself implies, with no model involved.

    For the scripted disruption this returns exactly what `agent.replan` runs
    today -- a Duffel search ZRH to MXP and an SBB search Zurich HB to Milano
    Centrale -- but it arrives there by derivation, so a disruption on a
    different leg gets its own answer instead of Milan's.

    Two adjustments the hard-coded version does not make. The window opens
    later by the ground time to the departure terminal, because a traveller who
    lands at 10:25 is not on a platform at 10:25. And it closes at the horizon
    rather than at the deadline, because giving up the target booking is a
    legitimate plan and the ranker, not the searcher, decides that.
    """
    out: list[Probe] = []
    for mode in MODES:
        starts = terminals(mode, gap.origin)[:2]
        ends = terminals(mode, gap.destination)[:2]
        for origin, walk in starts:
            for destination, _ in ends:
                got = check(mode, origin, destination, gap.not_before + walk,
                            gap.by + HORIZON, gap.not_before,
                            why="derived from the gap")
                if isinstance(got, Probe) and got.key not in {p.key for p in out}:
                    out.append(got)
    return out


SYSTEM = """You propose SEARCHES for a stranded traveller. You never propose \
flights, trains, prices or times of departure: you do not know them, and a \
provider does.

Reply with JSON only: {"probes": [{"mode": ..., "origin": ..., \
"destination": ..., "after_min": 0, "until_min": 0, "why": "..."}]}

  mode        "flight" or "rail"
  origin      a code from the vocabulary for that mode
  destination a code from the vocabulary for that mode
  after_min   minutes to shift the START of the search window, may be negative
  until_min   minutes to shift the END of the search window, may be negative
  why         at most eight words, for a human reading an audit trail

Both offsets are relative to the window you are given. Never write a date or \
a clock time; they are resolved for you.

The window you are given already runs past the deadline on purpose: arriving \
late and giving up the booking at the far end can be the better plan, and the \
ranker decides that, not you.

Propose at most four, and make them genuinely different from each other and \
from what has already been tried: a different mode, a different airport or \
station for the same city, or a materially wider window. A repeat of a search \
that returned nothing is a wasted call."""


def _near(a: Probe, b: Probe) -> bool:
    """Same route, same mode, windows within `NEAR`. Bucketing by a rounded key
    alone would call 10:25 and 10:36 different searches for landing either side
    of the quarter hour, which is an arbitrary reason to pay a provider twice.
    """
    return (a.mode == b.mode and a.origin == b.origin
            and a.destination == b.destination
            and abs(a.not_before - b.not_before) < NEAR)


def _ask(model, gap, tried: set[str], refused: list[Rejected]) -> list[dict]:
    import _model

    vocab = vocabulary()
    lines = [
        f"The traveller is at {places.label(gap.origin)} ({gap.origin}).",
        f"They must reach {places.label(gap.destination)} ({gap.destination}) "
        f"by {gap.by:%a %d %b %H:%M}.",
        f"The window starts {gap.not_before:%a %d %b %H:%M}.",
        f"Flight codes: {', '.join(vocab['flight'])}.",
        f"Rail station codes: {', '.join(vocab['rail'])}.",
    ]
    if tried:
        lines.append("Already searched: " + "; ".join(sorted(tried)) + ".")
    if refused:
        lines.append("These failed: " + " | ".join(r.hint for r in refused[-6:]) + ".")

    reply = _model.ask_json(model, SYSTEM, "\n".join(lines))
    probes = (reply or {}).get("probes")
    return probes if isinstance(probes, list) else []


def suggest(gap, model=None, tried: set[str] | None = None,
            refused: list[Rejected] | None = None,
            budget: Budget | None = None) -> tuple[list[Probe], list[Rejected]]:
    """One round of proposals: what to search next, and what was refused.

    Never raises and never returns nothing useful. A model that is absent,
    throttled, or answering with prose falls through to `seed`, which is the
    behaviour `agent.replan` has today -- the proposer can fail all the way
    back to the shipped product without the traveller noticing.
    """
    tried = tried or set()
    budget = budget or Budget()
    out: list[Probe] = []
    bad: list[Rejected] = []

    if not budget.take("rounds"):
        return [], [Rejected(None, Reason.OUT_OF_BUDGET, "no rounds left")]

    for raw in _ask(model, gap, tried, refused or []):
        if not isinstance(raw, dict):
            bad.append(Rejected(None, Reason.MALFORMED, "not an object"))
            continue
        try:
            after = timedelta(minutes=float(raw.get("after_min") or 0))
            until = timedelta(minutes=float(raw.get("until_min") or 0))
        except (TypeError, ValueError):
            bad.append(Rejected(None, Reason.MALFORMED, "offsets are not numbers"))
            continue

        got = check(str(raw.get("mode", "")).strip().lower(),
                    str(raw.get("origin", "")), str(raw.get("destination", "")),
                    gap.not_before + after, gap.by + until, gap.not_before,
                    str(raw.get("why", ""))[:60])
        if isinstance(got, Rejected):
            bad.append(got)
        elif got.key in tried or any(_near(got, p) for p in out):
            bad.append(Rejected(got, Reason.DUPLICATE))
        elif not budget.take("probes"):
            bad.append(Rejected(got, Reason.OUT_OF_BUDGET, "no probes left"))
            break
        else:
            out.append(got)

    if not out:
        # The floor. Only probes nobody has run yet -- falling back to a search
        # already known to be empty would turn an unhelpful model into an
        # infinite one.
        out = [p for p in seed(gap) if p.key not in tried]
    return out, bad


# ----------------------------------------------------------------- resolving

def resolve(probe: Probe, budget: Budget | None = None) -> Resolved | Rejected:
    """Run one probe against the real ports.

    THE FAILURE THIS EXISTS TO PREVENT. A proposal for a route nobody recorded
    raises PortError deep in `ports/base._replay`, and the date-shifted replay
    added a nearer hazard: a recording for another route with a matching shape
    is not what was asked for. Every path out of here is either real offers or
    a named refusal. There is no path where a probe quietly becomes a different
    search and the engine prices the answer to a question nobody asked.
    """
    budget = budget or Budget()
    if not budget.take("calls"):
        return Rejected(probe, Reason.OUT_OF_BUDGET, "no provider calls left")

    from base import PortError

    # ASK ON THE HOUR THE RECORDINGS WERE MADE ON, then filter to the probe's
    # own window below. The rail port keys its recording on the query time to
    # the minute, and the timetable returns what departs AFTER that time, so
    # asking at 10:37 -- which is when the traveller actually reaches the
    # platform -- misses the 06:00 capture entirely and rail vanishes from the
    # ranking. That is how this would have lost the plan the demo leads with.
    #
    # `flow.search_rail` already solved this and its comment says why: the hour
    # is not cosmetic, it decides which half of the day exists. The constant is
    # imported rather than repeated, because record.py and flow.py already have
    # to keep two copies equal and a third is where they stop being equal.
    from flow import RAIL_HOUR

    query = (probe.not_before.replace(hour=RAIL_HOUR, minute=0, second=0,
                                      microsecond=0)
             if probe.mode == "rail"
             else probe.not_before.replace(minute=0, second=0, microsecond=0))

    try:
        if probe.mode == "rail":
            import rail
            o, d = places.by_code(probe.origin), places.by_code(probe.destination)
            found = rail.offers(o.station, d.station, query,
                                places.station_map())
        else:
            import duffel
            found = duffel.offers(probe.origin, probe.destination, query,
                                  after=query)
    except PortError as exc:
        return Rejected(probe, Reason.PORT_UNAVAILABLE, str(exc)[:160])

    inside = [o for o in found if probe.not_before <= o.depart and o.arrive <= probe.by]
    if not inside:
        return Rejected(probe, Reason.NO_OFFERS,
                        f"{len(found)} departures, none arriving by {probe.by:%H:%M}")
    return Resolved(probe, inside)


def search(gap, model=None, budget: Budget | None = None) -> tuple[list, list[Rejected]]:
    """Propose, resolve, and try again while the budget and the reasons allow.

    The loop terminates on the counter, never on the model saying it is done.
    It also stops early when every refusal is a dead end -- another round of
    proposals cannot fix "not a place this system knows", and asking anyway is
    the difference between a bounded loop and a bounded loop that always runs
    to its bound.
    """
    budget = budget or Budget()
    tried: set[str] = set()
    refused: list[Rejected] = []
    offers: list = []

    while True:
        probes, bad = suggest(gap, model, tried, refused, budget)
        refused += bad
        if not probes:
            break
        for probe in probes:
            tried.add(probe.key)
            got = resolve(probe, budget)
            if isinstance(got, Resolved):
                offers += got.offers
            else:
                refused.append(got)
        if offers or budget.spent["rounds"] >= budget.rounds:
            break
        if not any(r.reason in WORTH_ANOTHER for r in refused):
            break

    seen, unique = set(), []
    for offer in offers:
        if offer.id not in seen:
            seen.add(offer.id)
            unique.append(offer)
    return unique, refused
