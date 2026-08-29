# Downstream — engine

Cross-provider disruption replanning. Detection is commodity; this is the part
that is not.

```bash
python serve.py                   # the web app, first free port from 8000
python cli.py                     # the same numbers, in a terminal
python cli.py --base written      # the October scenario, as the tests assert it
python mcp_server.py              # the engine as MCP tools, over stdio
python -m pytest tests/ -q        # 150 tests, no keys, no network
```

`/health` reports `ports_mode`, `model_status` and `storage`, and the front page
prints them. `model_status.ready` is the one worth reading: `label` is what was
configured and `ready` is whether it started — a Bedrock key with no model
access says "bedrock:claude-haiku-4-5" in confident blue otherwise, while every
call quietly falls back to a template.

Runs in **replay** by default: every API response is a committed fixture, so
the demo cannot be killed by a cold request or an expired token. To go live:

```bash
export RAPIDAPI_KEY=...  DUFFEL_TOKEN=...        # rail needs no key
DOWNSTREAM_PORTS=record python record.py         # overwrite fixtures for real
DOWNSTREAM_PORTS=live   python cli.py
export LLM_PROVIDER=bedrock                      # or anthropic, or leave unset
```

## What is computed and what is written

Nothing in `domain/ policy/ graph/ plan` contains a severity, a total or a
ranking that a human typed. `EUR 282`, `-EUR 14` and `EUR 176` appear only in
the tests that assert them. That is the whole point: the claim is checkable in
ten seconds by anyone leaning over a laptop.

The model is load-bearing in exactly two places — reading fare prose into
deadlines (`parse.py`) and writing the plan's rationale (`agent.py`). Both have
deterministic fallbacks. Arithmetic does not become unavailable when a token
expires.

## Known limits, stated rather than hidden

- **The rail fare is an estimate.** transport.opendata.ch returns timetables,
  not prices, and SBB publishes no free fare API. Every `Offer` carries
  `price_source`; rail is `"estimate"` and says so everywhere it appears.
- **Live flight status only covers about a week either side of today**, so a
  fixed October scenario cannot be demonstrated live in September. Anchoring
  the trip relative to run time is scheduled for days 9–10.


## The web app

`serve.py` is a translation layer over the engine and nothing more. Every figure on the page comes
from the same `propagate` and `generate` calls `cli.py` makes — if the page and the terminal ever
disagree, the page is wrong.

Five stages, matching the CLI: the signal, what it costs, the knock-on impact, the ranked plans, and
the handoff. Two things it refuses to hide:

- **`degraded`** — a live call failed and a recording was replayed.
- **`shifted`** — a recording was replayed under a different date because the trip is anchored to
  run time. Both appear in the banner and in `/health`.

Times are formatted server-side in the trip's own timezone. Letting the browser format them renders
a Zurich arrival in the viewer's zone, which is a different flight as far as the traveller is
concerned.


## Telling it what you want

`/` is the front door: a sentence, not a form.

    "book me a flight from singapore to london from 1 to 7 september"

`converse.py` is a LangGraph — read, classify, fill, ask, route, search, offer —
and every node except the model call is a pure function of the state. It asks
only what changes the answer: a hotel decides whether LiteAPI is called at all,
and "cheapest, fastest or direct" orders the options now *and* orders the
recovery plans if this trip breaks in a week. Seat preference changes nothing
here, so it is not asked, however conversational it would sound. Every question
carries the reason it is being asked, on screen.

Parsing runs deterministically first — cities, dates, party size, the words
"direct" and "cheapest" — and whatever it resolves is final. The model is asked
only about what is still blank, which means a model having an imaginative day
cannot move a date the traveller stated plainly, and the whole thing still runs
with `LLM_PROVIDER=none`.

The page names the providers it called. Book as many itineraries as you like,
then break any one of them from the panel and take a plan.

## Acting on a plan

`act.py` is the part that makes it adaptation rather than notification.

- **AUTO** is performed. The message is sent, and the result records which
  channels actually took it — possibly none, which is shown rather than hidden.
- **TAP** and **CALL** are handed over with a link and marked pending. Nothing
  is bought: Duffel's test mode cannot take money and we would not use it if it
  could.
- **The itinerary is rewritten.** The cancelled leg goes, the replacement
  arrives flagged `pending`, and that flag survives storage — a real booking in
  the plan and an intention in the record are not allowed to look alike.

## Buying a trip, then breaking it

`/book` is the same engine with nothing scripted. Search real inventory (Duffel
for flights, LiteAPI for stays — both on accounts that cannot take money), pick
an outbound, an onward leg and a room, then press **Cancel this flight** on any
leg and watch the consequences come out of live data. `flow.py` is the whole
loop: `search_flights`, `search_hotels`, `select`, `cancel`, `replan`.

Nothing about the scenario is configured. The recovery query — where the
traveller now stands, where they are contractually due next, by when — is
derived in `plan.recovery_gap`, so cancelling a different leg searches a
different route without an edit. Four things this flow made visible that a
literal trip had hidden:

- **A cancelled flight is not its own replacement.** The cancellation is ours,
  not the airline's, so its seats are still in inventory and the search returns
  them. Filtered by designator, airports and departure minute.
- **Downstream is a question about deadlines, not start times.** Walking by
  start time charged a EUR 1,031 long-haul that landed that morning to the
  cancellation of a EUR 170 onward hop.
- **A stay is where the hotel is.** Inferring it from the itinerary filed a
  Milan hotel under Zurich the moment the onward leg landed the next morning.
- **One ledger for every plan, inaction included.** A room defusable by a phone
  call was free under "do nothing" and charged in full to every alternative, so
  the ranking compared two different ledgers.

The old scripted demo — one late flight, eleven bookings, EUR 282 — still runs
at `/`, from the same fixtures, asserting the same numbers.

## The watch

An engine you have to open is a report. `monitor.py` is the part that speaks
first: `schedule(trip, disruption, now)` returns every moment somebody has to be
told something, at T-60, T-15 and the deadline itself.

An alert exists only where the clock running out takes something away — a fare
window still worth more than it costs to use, or the last moment a replacement
could still land in time. A deadline with nothing behind it is a fact, not an
alert, and stays in the impact graph. Where two deadlines land on the same
booking at the same minute (the museum's EUR 18 date change and the museum
itself both close at 16:00) the larger loss wins, because one clock deserves one
sentence.

Nothing is queued. The schedule is a pure function of the trip and the clock, so
the same call answers "what is coming" for the page and "what is due" for the
ticker; firing once comes from a watermark stored on the trip, which is what
makes *once* survive a restart or a second worker.

```bash
export DOWNSTREAM_WEBHOOK=https://hooks.slack.com/...   # or any JSON POST endpoint
export DOWNSTREAM_WATCH=0                               # turn the loop off
```

`notify.deliver` returns the channels that actually took the message, and an
empty list is a real answer the page is obliged to show. Email is deliberately
absent: it needs a verified sending domain, and a mailer that lands in spam
looks delivered on this end. The loop runs in-process, which is real while the
service is awake and stops when a free instance sleeps — `/health` says so
rather than leaving it to be discovered.

## Dates

Everything anchors to **today** by default. Live flight status only covers about a week either side
of now, so a trip pinned to 12 October cannot be demonstrated live in September — "as written" is
the special case, reachable with `--base written` or `?base=written`, and it is what the tests
assert against because those figures can be checked by hand.

`--base` takes `today`, `written`, `+3`, `-1`, or a date. Recordings made on a different day are
replayed with every date inside them shifted to match, and each shift is announced in the banner and
in `/health`. When one route has been recorded more than once, a caller that knows which capture it means
names it — the scripted scenario asks for the October one, so its figures do not move when the
trip is anchored to a different week. Everything else takes the recording nearest the day being
asked about, because the shift is the distortion and the smallest one is the least wrong. Note
what the date in a fixture name is: the day the search was *for*, not the day it was captured,
which is why "newest file" justifies nothing. Only a route with no recording at all still
refuses — that is the failure re-recording actually fixes.

## MCP

The engine is a tool other agents can call. Detection is commodity; cross-provider replanning is
not, and an MCP surface is how that becomes somebody else's building block rather than our demo.

```json
{"mcpServers": {"downstream": {
    "command": "python", "args": ["/absolute/path/to/mcp_server.py"]}}}
```

Three tools: `itinerary` (what the trip is, including the two ticket groups that make nobody
responsible for the connection), `impact_of` (what a delay breaks and which deadline falls first),
and `recovery_plans` (ranked options). Two things travel with the output on purpose — every action's
**lane**, because a `call` action has no consumer API and an agent reporting it as done has lied to
a stranded traveller; and every fare's **price_source**, because the Swiss rail number is ours
rather than a quote.
