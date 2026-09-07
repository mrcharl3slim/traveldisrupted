# TripShield

**Cross-provider travel disruption replanning.** One flight is late. Eleven
bookings across five suppliers are affected, none of whom talk to each other,
and some of the money is still recoverable — but only for the next few hours.
TripShield works out which bookings break, what that costs, what the cheapest
recovery is, and what it is allowed to do about it on the traveller's behalf.

Detection is commodity — every airline app tells you your flight is late.
The part that is not commodity is the sentence after that: *what does that do
to the rest of the trip, and what should happen now.*

**Nothing in this system asserts a number.** Severities, totals, deadlines and
plan rankings are all derived from typed data by pure functions. If a figure is
wrong, the model of the world is wrong — which is the correct place for the
argument to happen.

---

## Live

**https://downstream-0h5s.onrender.com** — nothing to install. Running in
`live` mode against the real providers, on Bedrock, with Postgres behind it.

It is a free Render instance, so it sleeps when idle and takes about a minute to
wake. **Open it a few minutes before demoing**, and read `/health` first:

```json
{"ok": true, "ports_mode": "live", "degraded": [], "shifted": [],
 "model_status": {"label": "bedrock:claude-haiku-4-5", "ready": true},
 "storage": "postgres", "storage_status": {"durable": true},
 "watch": {"running": true, "every_seconds": 60}}
```

Three fields are worth reading before you trust the page. `degraded: []` means
every provider answered and nothing fell back to a fixture. `ready: true` means
the model actually started, not merely that a key was set — a Bedrock key
without model access says `bedrock:...` in confident blue while every call
quietly falls back to a template, and this is the field that catches it.
`storage: postgres` with `durable: true` means a trip pasted now survives a
restart.

The deadline watch reports `durable: false` and says why: it runs in-process, so
on a free instance it stops when the instance sleeps. On this plan it is a
demonstration of the mechanism rather than a promise to a traveller.

---

## 1. Run it

Needs **Python 3.11** (3.10 works). No API keys. No network. No database.

```bash
cd downstream
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then any of:

```bash
python cli.py                  # the whole engine, in a terminal, ~1 second
python serve.py                # the web app  ->  http://127.0.0.1:8000
python -m pytest tests/ -q     # 516 passed, 10 skipped, no keys, no network
python mcp_server.py           # the engine as MCP tools, over stdio
```

`cli.py` deliberately needs **no dependencies at all** — `domain / policy /
graph / plan` are standard library only, so the arithmetic a judge is asked to
believe does not depend on a wheel resolving. It runs on a clean interpreter:

```bash
python3 cli.py                 # works before you install anything
```

`serve.py` picks the first free port from 8000 and prints the URL. The 10
skipped tests are the Postgres ones (no `DATABASE_URL`) and one that needs a
duplicate fare brand that is not in the recorded search — nothing is hidden.

### Default mode: replay

With no `.env` at all the app runs in **replay** — every provider response is a
committed fixture in `fixtures/`. No network, no latency, no rate limit, no
expired token. This is the mode the demo runs in, and it is a design decision
rather than a fallback: a live demo that a third party's uptime can kill is not
a demo. The ports parse a recording exactly as they parse a live response, so
the numbers are identical either way.

Fixtures are dated September and October 2026; `--base` shifts them to run
time so the countdown is always live, and the CLI header names every shift it
applied.

### Optional configuration

```bash
cp .env.example .env            # everything in it is optional
```

`env.py` folds `.env` into the environment before anything reads it. Existing
environment variables always win.

| Variable | Default | What it changes |
|---|---|---|
| `DOWNSTREAM_PORTS` | `replay` | `replay` \| `live` \| `record` |
| `LLM_PROVIDER` | `none` | `bedrock` \| `anthropic` \| `groq` \| `none` |
| `RAPIDAPI_KEY` | — | AeroDataBox flight status (sold via RapidAPI) |
| `DUFFEL_TOKEN` | — | Replacement flights, test mode |
| `LITEAPI_KEY` | — | Hotels, sandbox |
| `DATABASE_URL` | — | Postgres; without it trips live in memory |
| `DOWNSTREAM_TIMEOUT` | `8` | Seconds a provider gets before we replay instead |
| `DOWNSTREAM_WATCH` | `1` | In-process deadline monitor; set `0` to disable |
| `DOWNSTREAM_WEBHOOK` | — | Slack/Discord/any JSON webhook for alerts |
| `DOWNSTREAM_ADMIN_TOKEN` | — | Enables `POST /api/admin/wipe`; 404 until set |
| `DOWNSTREAM_ADB_DAILY_BUDGET` | `120` | Caps AeroDataBox calls against the free tier |
| `FX_EUR_SGD` etc. | fixed | Override the declared conversion rates |

Swiss rail needs no key. To go live:

```bash
export RAPIDAPI_KEY=... DUFFEL_TOKEN=... LITEAPI_KEY=...
DOWNSTREAM_PORTS=record python record.py     # refresh fixtures from real APIs
DOWNSTREAM_PORTS=live   python cli.py
```

In `live` mode a failed call **falls back to the recording and says so** —
`/health` and the page both report `degraded` with the port that fell back. A
visible degradation beats an invisible one.

---

## 2. What to look at

Start the server and open these in order. Six minutes, in this sequence, is the
demo.

| Route | What it is |
|---|---|
| `/` | The conversational front door. Type a trip in plain English. |
| `/demo` | The scripted delay — one late flight, eleven bookings, the ledger. The tests assert this page to the dollar. |
| `/story` | The **whole** capability as one scripted trip, run server-side through the same handlers every button calls, so it cannot drift from what a person clicking would get. Re-runnable. |
| `/script` | The demo as a script: seven acts, every line to type and every line to say, plus an "if it goes sideways" table. Reads `/health` first. |
| `/book` | Build a trip from live inventory, then break it. Nothing scripted. |
| `/trip` | One itinerary: every leg, what is thin, the rules, the paper trail, who else can see it. |
| `/test` | The page to hand anybody who did not build this — what to try, what should happen, which limits are deliberate. |
| `/developer` | The API surface. |
| `/health` | `ports_mode`, `model_status`, `storage`. Read this before demoing. |

`/health`'s `storage` has three states, not two: `postgres`, `memory` because
nobody asked for a database, and `memory` **because one was asked for and is
not answering** — with the driver's own words for why.

### What `python cli.py` prints

```
  ports: replay
  dates: anchored to 06 Sep 2026  (status: replayed
         flight-SQ346_on-2026-10-11.json shifted -35 days; duffel: ...; rail: ...)

  SIGNAL   SQ 346 - Singapore to Zurich
           delayed 3:50:00, arrives 10:25 (was 06:35), confidence 91%

  BOOKING                           SEVERITY     EXPOSED  RECOVER   CUTOFF
  SQ 346 - Singapore to Zurich      source
  LX 1608 - Zurich to Milan Malpens critical         213       57   07 Sep 08:20
  Malpensa to hotel private transfe critical          72       72   07 Sep 06:00
  Hotel Le Marais Milano - 3 nights risk             936
  The Last Supper - timed entry     critical         138      111   07 Sep 16:00
  ...

  DO NOTHING       S$423 lost across 3 bookings
  ACT IN TIME      S$240 recoverable
  NEXT DEADLINE    06:00 - Malpensa transfer (in 3:22:00)

  PLAN                                           TONIGHT   DAMAGE   ARRIVES
  EC 11:33 - Zürich HB to Milano Centrale, ch        -21      264   MILANO_C 15:17 *
  ET 0737 - ZRH to MXP                              +140      353   MXP 22:10 +
  Do nothing                                           0      423   -

  * fare estimated - no reachable API sells Swiss rail tickets
  + fare quoted in another currency, converted at a fixed rate
```

Two numbers from one ledger, and they answer different questions:
`net_cash = out − in` is what the traveller pays tonight; `total_damage =
wasted + net_cash` is what the disruption costs in total. Plan B ranks first at
a **negative** cash flow without anyone putting a thumb on the scale — acting
is cheaper than not acting, and that falls out of the arithmetic.

---

## 3. How it is put together

```
                    ingest.py / request.py / converse.py / appointment.py
   what a person       ↓  (the model reads prose here)
   says or pastes   builder.py / flow.py  →  domain.Trip
                        ↓
                    ┌─────────────────────────────────────────┐
                    │  THE ENGINE — stdlib only, no deps      │
   the arithmetic   │  domain · policy · graph · plan · money │
   a judge checks   │  risk · whereabouts · monitor           │
                    └─────────────────────────────────────────┘
                        ↓
                    permit.py  (may we?)  →  act.py (do it) → audit.py
                        ↓
                    agent.py (LangGraph)   serve.py (FastAPI)   mcp_server.py
                        ↓
                    ports/  ──  replay | live | record  ──  fixtures/
```

**Four boundaries do the work:**

1. **Description vs. judgement.** `domain.py` holds only nouns and is
   deliberately logic-free, because descriptions come from a model parsing
   emails and judgements have to be defensible arithmetic. Keeping them in
   separate files keeps *"the model guessed this"* visibly apart from
   *"we computed this."*
2. **Ports.** Every provider sits behind `ports/base.py` in one of three modes,
   so a live API cannot kill a demo and CI needs no keys.
3. **CAN vs. MAY.** `plan.Lane` is a fact about the *supplier* — can this be
   done without a human? `permit.py` is a fact about the *traveller* — may we?
   Nothing is autonomous because the software is capable of it, only because
   the traveller allowed it.
4. **One currency.** Conversion happens once, at `money.py`, at the boundary
   where a quote enters. Everything downstream is S$, and anything converted
   says so.

### Where the model is — and deliberately is not

The model is used **only where language is the input or the output** — reading
prose a traveller or a supplier wrote, and writing a sentence back. It is never
used for arithmetic, and that line falls cleanly between files: `ingest.py`,
`request.py`, `converse.py`, `appointment.py`, `propose.py` and `flow.py` all
call a model; `domain`, `policy`, `graph`, `plan`, `money`, `risk`,
`whereabouts` and `monitor` contain no model call at all.

Two of those uses carry most of the weight:

- **`parse.py`** — *"free change up to 4 hours before pickup"* → `12 Oct 06:00`.
  Reading the sentence, knowing which booking it modifies, doing arithmetic on
  that booking's own times. No travel API supplies this. **This is the moat.**
- **`agent.py: explain`** — the plan's rationale, in two sentences.

Inside the LangGraph pipeline, four of the five nodes — detect, propagate,
replan, handoff — are pure functions over typed data.
An agent that *reasons about* whether S$72 is recoverable will eventually reason
wrongly, on stage, about the number the pitch rests on. Both model calls have
deterministic fallbacks, and there is a test that raises `ThrottlingException`
and asserts the plan survives intact. `LLM_PROVIDER=none` is a supported mode,
not a broken one.

`parse.py` also refuses absolute dates from the model — it demands offsets
(*"4 hours before start"*) and resolves them in Python. A model allowed to write
`2026-10-12T06:00` can silently write the wrong *day* and nothing downstream
would catch it. Unparseable rules become non-refundable: failing toward *"you
can't get this back"* under-promises rather than inventing a refund.

### Import layout

`cli.py`, `serve.py` and `tests/conftest.py` put `./`, `./data` and `./ports` on
`sys.path`, so port modules import as top-level names (`import rail`) and the
engine never learns where its data came from.

---

## 4. Every file, and why it exists

### The engine — standard library only

| File | Purpose |
|---|---|
| `domain.py` | The nouns: `Booking`, `Trip`, `Disruption`, `Kind`, and the local `transit` table. Logic-free on purpose. |
| `policy.py` | `Window` and `Policy` — what a booking is worth *at a given moment*, and when that stops being true. Windows are stored already resolved to absolute datetimes, because a relative rule is ambiguous the moment the booking it hangs off moves. |
| `graph.py` | **The file that turns a delay into a number.** Propagates a disruption through the itinerary. One central relation: a booking survives if `presence(place, day) + transit(place → booking) ≤ booking.must_arrive_by`. Severity, exposure and totals all fall out of it. |
| `plan.py` | Recovery plans, *generated and scored* rather than written. One ledger yields both money numbers. Also `Lane` — AUTO / TAP / CALL — and `recovery_gap`, the query the engine needs answered. |
| `money.py` | The single currency boundary. Fixed, declared rates; everything converted is labelled `converted` and keeps the provider's own words. |
| `risk.py` | Where the trip is thin, said *before* anything breaks. Thin connections, unreachable appointments, single points of failure — all derived. No weather feed, no probability model, no prediction. |
| `whereabouts.py` | Where the traveller is at every moment, derived from the itinerary alone. |
| `monitor.py` | Deadlines, and when somebody has to be told. One alert = one deadline with money or a bed behind it. An engine you have to open is a report; one that speaks first is a service. |

### Getting real input in

| File | Purpose |
|---|---|
| `ingest.py` | Confirmation emails → `Booking`s. Every operator invents its own layout, so this is a model job with a validating fallback. |
| `parse.py` | Fare prose → resolved deadlines. The moat (see above). |
| `request.py` | What the traveller asked for and what is still missing. One rule decides what gets asked: *does the answer change what the system does?* |
| `converse.py` | The conversation as a graph — `read → classify → fill → ask → route → search → offer`. Every node pure except one, and that one is fenced. |
| `appointment.py` | Things the traveller must attend that nobody sold them. Modelled as a `Booking` with no price and `commitment=True`, so the whole engine handles it with no second code path. |
| `builder.py` | Chosen offers → a `Trip`. Separate purchases become separate ticket groups, so the scenario's central flaw (two tickets, nobody owes the connection) falls out of the data rather than being encoded. |
| `flow.py` | The loop a traveller actually goes round: search → select → cancel → replan. Recovery is priced by the same market that sold the original. Deliberately does not pay. |
| `propose.py` | What to search for when the answer is not one search. `plan.recovery_gap` yields one query; this turns it into the searches nobody wrote down — fly into the other airport and take the ground link, leave from the station in the city you are already in — and gives every rejected candidate a reason. |
| `places.py` | City names → airport codes, countries and timezones. A geocoder in a dict, and named as one. Unknown places return `None` and the agent asks rather than guessing. |
| `profile.py` | Who the traveller is, kept across trips. Consent-based, and only the fields the engine actually reads. |

### Agency, permission and proof

| File | Purpose |
|---|---|
| `permit.py` | What the agent may do on its own, as the traveller defined it. The **MAY** half of the pair. |
| `act.py` | Actually doing it, and being exact about which part was done. A plan that recommends emailing a hotel and then does not email the hotel is a report with buttons on it. |
| `notify.py` | Getting an alert to a person and never falsely claiming to have. Returns the channels that actually took it; an empty list is a real answer the page must show. |
| `audit.py` | Append-only paper trail: what was observed, decided, done — and on whose say-so. Written at decision boundaries, not at function boundaries. |
| `roles.py` | Who may see and do what on a trip. **There is no login** — identity is a token in a shared link, and that is stated plainly rather than dressed up. |
| `agent.py` | The LangGraph pipeline: `detect → propagate → replan → explain → handoff`. Four arithmetic nodes and one that thinks. |
| `_model.py` | One switch picks the provider: Bedrock, Anthropic, Groq or none. The agent never learns which it got. |

### Providers

| File | Purpose |
|---|---|
| `ports/base.py` | Record / replay / live, plus date-shifting so October fixtures answer September questions. The posture that makes a live demo safe. |
| `ports/aerodatabox.py` | Flight status via RapidAPI. Free tier 600 units/month, metered against a daily budget. Only has opinions ±7 days from today. |
| `ports/duffel.py` | Replacement flights. Test mode is free and really does quote a fare, so its offers are `price_source="quoted"`. (Amadeus Self-Service was decommissioned 17 Jul 2026.) |
| `ports/rail.py` | Swiss rail via transport.opendata.ch. Free, no key, real timetable — **and no fares**, which is stated everywhere it matters. |
| `ports/chinarail.py` | China Railway via 12306's own front-end endpoint. Read-only timetables, no fares. Documented as a demo source and a seam to replace, not a licence. |
| `ports/hotels.py` | Hotels via LiteAPI, in two calls (catalogue, then rates). Not Duffel Stays, whose test hotels all sit at one point in the Pacific. |

### Web, tools and data

| File | Purpose |
|---|---|
| `serve.py` | FastAPI over the engine — 37 routes. A translation layer only: it never recomputes, never hides a degradation, never pretends. If this and `cli.py` disagree, this one is lying. |
| `static/agent.html` | `/` — the conversational front door. |
| `static/index.html` | `/demo` — the scripted delay the tests assert. |
| `static/story.html` | `/story` — the whole capability, scripted, server-side. |
| `static/script.html` | `/script` — the demo script for the presenter. |
| `static/book.html` | `/book` — build a trip from live inventory, then break it. |
| `static/trip.html` | `/trip` — one itinerary in its own window. |
| `static/test.html` | `/test` — the guide for people who did not build this. |
| `static/developer.html` | `/developer` — the API surface. |
| `static/app.css`, `static/app.js` | Shared styling and the small amount of shared script. |
| `mcp_server.py` | The engine as MCP tools over stdio, so *any* agent can ask what one delay costs. The claim is not "we built an agent" but "the replanning engine is a tool other agents can call." |
| `cli.py` | The impact map in a terminal. Exists so *"computed, not typed"* can be checked in ten seconds. `--base today \| +N \| YYYY-MM-DD \| written`. |
| `store.py` | Where trips live between requests. Memory by default; Postgres when `DATABASE_URL` is set. An unreachable database degrades rather than 500s. |
| `data/demo_trip.py` | The 11–17 October trip as structured facts. Contains no consequence — no severity, no total, no plan. If any appear here, the demo has become a mockup again. |
| `data/offers.py` | Seed replacement offers in the exact shape the ports return, so `plan.py` never learns where they came from. |
| `fixtures/` | Committed provider responses in each API's real wire shape. Evidence, and the reason CI needs no keys. |

### Operations

| File | Purpose |
|---|---|
| `env.py` | Folds `.env` into the environment once, before anything reads it. |
| `record.py` | Overwrite fixtures with real responses. Needs network and keys; run it outside any sandboxed shell. |
| `trim_fixtures.py` | One-off: shrink fixtures already on disk using the same rules. |
| `maintain.py` | Clear stored data from wherever `DATABASE_URL` points. Dry-run unless `--yes`. `trips` keeps people and profiles; `everything` does not. |
| `render.yaml` | Render blueprint, so the deployment is reviewable in the repo instead of in a browser tab. |
| `requirements.txt` | Agent and API layer only — the engine needs none of it. `boto3` is declared rather than relied on as a transitive of `langchain-aws`, and `httpx` is pinned although nothing imports it directly: Starlette's `TestClient` needs it, and an HTTP suite that silently skips is how two ship-blocking bugs once reached a deployed instance behind a green run. Every provider the switch in `_model.py` offers is declared here, groq included: a provider that needs a manual `pip install` is one that works on the machine where somebody read this table and nowhere else. |
| `.env.example` | The settings you are most likely to change — port mode, provider keys, model, storage — with where to get each key. The operational knobs (`DOWNSTREAM_WATCH`, `DOWNSTREAM_WEBHOOK`, `DOWNSTREAM_ADMIN_TOKEN`, `FX_*`) are documented in the table in §1 rather than here. |
| `CAPABILITIES.md` | The long-form walkthrough of every capability, feature by feature. |

### Tests — 516 pass with no keys and no network

`tests/conftest.py` sets up the import path; everything else is a suite.

| File | Covers |
|---|---|
| `test_engine.py` | The core propagation arithmetic. |
| `test_plans.py` | Plan generation, the ledger, and the ranking. |
| `test_api.py` | The HTTP layer — the largest suite, and the reason `httpx` is a declared dependency rather than an accident of somebody's virtualenv. |
| `test_ports.py` | Record/replay, fallback, date shifting — and that every third-party import is declared in `requirements.txt`, derived from the imports rather than from a list, so the next undeclared one fails here instead of on somebody's fresh checkout. |
| `test_agent.py` | The LangGraph nodes, including that the plan survives a throttled model. |
| `test_act.py` | Performing actions and reporting exactly what was performed. |
| `test_permit.py` | The permission gate. |
| `test_roles.py` | Token identity, sharing and redaction. |
| `test_audit.py` | The paper trail. |
| `test_ingest.py` | Confirmation prose → bookings. |
| `test_converse.py` | The conversation graph. |
| `test_propose.py` | Which searches to run when the answer is not one search. |
| `test_appointments.py` | Commitments nobody sold the traveller. |
| `test_flow.py` | Search → select → cancel → replan. |
| `test_cancel.py` | Whole-trip abandonment, priced. |
| `test_monitor.py` | Deadline scheduling and firing. |
| `test_watch_budget.py` | The provider call budget. |
| `test_risk.py` | Pre-disruption fragility flags. |
| `test_whereabouts.py` | The presence timeline. |
| `test_store.py` | Memory and Postgres stores (Postgres tests skip without `DATABASE_URL`). |
| `test_profile.py` | Cross-trip preferences. |
| `test_dates.py` | Base-date anchoring and fixture shifting. |
| `test_story.py` | The `/story` walkthrough, end to end. |
| `test_mcp.py` | The MCP tool surface. |
| `test_urls.py` | Deep links resolve. |

---

## 5. Limits, stated rather than hidden

These are in the README because a judge finding them is worse than us saying
them.

- **Swiss rail fares cannot be quoted.** transport.opendata.ch returns
  timetables, not prices, and SBB publishes no free fare API. The rail fare
  under the winning plan is *our estimate*, marked `estimate` everywhere it
  appears, with a deep link so the traveller sees the true number before paying.
- **Conversion rates are fixed guesses**, declared as such, applied once at
  `money.py`. A real deployment reads a rates feed.
- **Live flight status is ±7 days.** No status API has an opinion today about a
  flight in five weeks, which is why the trip anchors to run time.
- **Recovery never returns everything.** Of the total damage, only part is
  catchable, and only before the deadlines pass. The honest line is *"S$423
  evaporates, S$240 of it is still catchable for the next 3 h 22 m."*
- **A moved timed-entry slot is not re-checked against the rest of the day.**
  When a plan reschedules a fixed slot, the engine prices the move but does not
  walk the rewritten itinerary again — so Plan A's 11:30 museum entry can land
  on the same day as the 09:15 Como tour without the clash being reported.
  `builder.clashes` already computes exactly this and is wired only into the
  appointment path; the work is running it over the rewritten trip inside
  `plan.build` and ranking on the result, which is a day and not a rewrite.
- **There is no login.** A shared link contains a token, and a token can be
  copied. That is the honest shape for a product with no accounts.
- **Nothing is paid for.** Duffel test mode and the LiteAPI sandbox return real
  schedules and real prices and cannot take money.
- **12306 is unofficial.** `ports/chinarail.py` speaks the endpoint the site's
  own front end uses. Treat it as a demo source.
- **On a free Render instance** the deadline watch is a demonstration of the
  mechanism, not a promise to a traveller — the instance sleeps when idle.
  Open the URL a few minutes before demoing.

---

## 6. Deploying

`render.yaml` is a Render blueprint — import it rather than clicking through the
dashboard. It runs `pip install -r requirements.txt`, starts `python serve.py`,
health-checks `/health`, and sets `DOWNSTREAM_PORTS=live` (falling back to
fixtures on any failure, visibly). Set `DOWNSTREAM_PORTS=replay` for a demo that
must not depend on anybody else's uptime.

Free instances sleep when idle and take about a minute to wake. The instance at
**https://downstream-0h5s.onrender.com** is deployed from this blueprint and
runs `DOWNSTREAM_PORTS=live`; `DOWNSTREAM_WEBHOOK` is unset there, so alerts go
to the log rather than to Slack, which `/health` reports as
`channels: {log: true, webhook: false}`.
