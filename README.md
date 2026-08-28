# Downstream — engine

Cross-provider disruption replanning. Detection is commodity; this is the part
that is not.

```bash
python cli.py                     # status -> impact -> ranked plans -> handoff
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
