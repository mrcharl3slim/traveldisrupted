"""One currency inside the engine, and it is Singapore dollars.

The engine sums money, and a sum across currencies is a number in no currency
at all. So conversion happens ONCE, at the boundary where a provider's quote
enters the system, and everything downstream -- the ledger, the ranking, the
cap the agent acts within -- is S$.

THE RATES ARE FIXED AND THEREFORE GUESSES, and everything converted says so:
`price_source` becomes "converted" and `quoted` keeps the provider's own words
("EUR 142"), exactly the honesty the rail estimate already had. A real
deployment reads a rates feed; a demo that pretended to would be lying about
the one thing this product exists to be straight about. Overridable by env
(`FX_EUR_SGD=1.48`) so a deployment can at least pin the day's truth.

EUR is deliberately 1.50 rather than the mid-market ~1.45: the scripted demo's
whole argument is figures a judge can check by hand, and EUR 282 x 1.5 = S$423
survives mental arithmetic where x 1.45 does not. The rate is a declared demo
constant either way; declared round beats declared awkward.
"""

from __future__ import annotations

import os

HOME = "SGD"
SIGN = "S$"

#: What one unit of each currency is worth in S$.
RATES: dict[str, float] = {
    "SGD": 1.0,
    "EUR": float(os.environ.get("FX_EUR_SGD", "1.50")),
    "USD": float(os.environ.get("FX_USD_SGD", "1.30")),
    "GBP": float(os.environ.get("FX_GBP_SGD", "1.75")),
    "CHF": float(os.environ.get("FX_CHF_SGD", "1.60")),
}


def to_home(amount: float, currency: str) -> tuple[float, str, str]:
    """A provider's quote -> (S$, price_source, what the provider said).

    A currency with no rate is not converted at gunpoint: the amount passes
    through marked "estimate" with the original named, because a wrong number
    labelled honest beats a made-up rate labelled exact.
    """
    currency = (currency or HOME).upper()
    if currency == HOME:
        return round(amount, 2), "quoted", ""
    rate = RATES.get(currency)
    if rate is None:
        return round(amount, 2), "estimate", f"{currency} {amount:,.0f} (no rate)"
    return round(amount * rate, 2), "converted", f"{currency} {amount:,.0f}"


def fmt(amount: float) -> str:
    """S$423 -- the way the brief writes it, everywhere a sentence needs it."""
    return f"{SIGN}{amount:,.0f}"
