# Downstream — engine

Cross-provider disruption replanning. Detection is commodity; this is the part
that is not.

```bash
python serve.py                   # the web app, first free port from 8000
python cli.py                     # the same numbers, in a terminal
python cli.py --base written      # the October scenario, as the tests assert it
python mcp_server.py              # the engine as MCP tools, over stdio
python -m pytest tests/ -q        # 48 tests, no keys, no network
```

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


## Dates

Everything anchors to **today** by default. Live flight status only covers about a week either side
of now, so a trip pinned to 12 October cannot be demonstrated live in September — "as written" is
the special case, reachable with `--base written` or `?base=written`, and it is what the tests
assert against because those figures can be checked by hand.

`--base` takes `today`, `written`, `+3`, `-1`, or a date. Recordings made on a different day are
replayed with every date inside them shifted to match, and each shift is announced in the banner and
in `/health`. Ambiguity refuses: if two recordings differ only by date there is no way to know which
was meant, and guessing would answer with the wrong day's flight.

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
