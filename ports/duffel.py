"""Replacement flights, via Duffel.

Amadeus Self-Service was decommissioned on 17 July 2026 -- portal closed, keys
deactivated -- so the familiar "just use the free Amadeus tier" answer is dead.
Duffel is the consumer-reachable replacement, and its test mode is free.

Unlike rail, this one really does quote a fare, so Offers from here are
``price_source="quoted"``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from base import call, get_json

BASE = "https://api.duffel.com/air/offer_requests"

#: The engine sums money. Duffel quotes in the account's currency — USD on a
#: test account — while the itinerary is in EUR, and adding the two produced a
#: total in no currency at all. Converting at the boundary keeps one unit in the
#: engine; the rate is fixed and therefore a guess, so anything converted says
#: so in `price_source` exactly as the rail estimate does.
TRIP_CURRENCY = os.environ.get("TRIP_CURRENCY", "EUR").upper()
FX = {"USD": float(os.environ.get("FX_USD_EUR", "0.92")),
      "GBP": float(os.environ.get("FX_GBP_EUR", "1.17")),
      "CHF": float(os.environ.get("FX_CHF_EUR", "1.06")),
      "EUR": 1.0}


def _to_trip_currency(amount: float, currency: str) -> tuple[float, str, str]:
    """(amount, price_source, what the provider said)."""
    currency = (currency or TRIP_CURRENCY).upper()
    if currency == TRIP_CURRENCY:
        return amount, "quoted", ""
    rate = FX.get(currency)
    if rate is None:
        return amount, "estimate", f"{currency} {amount:,.0f} (no rate)"
    return round(amount * rate, 2), "converted", f"{currency} {amount:,.0f}"

# Duffel timestamps are local wall-clock with NO offset -- "2026-10-12T15:30:00"
# means half past three in Zurich, and Python will happily compare that to an
# aware datetime by raising. The itinerary is entirely tz-aware, so the zone has
# to be reattached here, at the boundary, rather than leaking naive datetimes
# into the engine. Only the airports this demo touches; a real deployment reads
# the zone from the airport reference data Duffel already returns.
AIRPORT_TZ = {"ZRH": 2, "MXP": 2, "SIN": 8, "FLR": 2}


def _zone(place: dict | None):
    """The airport's own zone, from Duffel's reference data, if it is there.

    The table above covers the four airports this scenario touches, which was
    fine while the route was fixed. Now that any leg can be cancelled and any
    replacement searched, an unknown airport would silently borrow the zone of
    the day being searched — a Bangkok departure stamped as Swiss time, off by
    five hours, wrong in a way that looks plausible.
    """
    name = (place or {}).get("time_zone")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:            # unknown zone name, or no tzdata on the host
        return None


def _aware(stamp: str, iata: str, fallback, place: dict | None = None):
    t = datetime.fromisoformat(stamp)
    if t.tzinfo is not None:
        return t
    zone = _zone(place)
    if zone is not None:
        return t.replace(tzinfo=zone)
    hours = AIRPORT_TZ.get(iata)
    return t.replace(tzinfo=timezone(timedelta(hours=hours)) if hours is not None
                     else fallback)


def _headers():
    token = os.environ.get("DUFFEL_TOKEN", "")
    return {"Authorization": f"Bearer {token}",
            "Duffel-Version": "v2",
            "Accept": "application/json",
            "Content-Type": "application/json"}


def search(origin: str, destination: str, day: datetime, passengers: int = 1):
    key = {"from": origin, "to": destination, "on": f"{day:%Y-%m-%d}"}
    payload = json.dumps({"data": {
        "slices": [{"origin": origin, "destination": destination,
                    "departure_date": f"{day:%Y-%m-%d}"}],
        "passengers": [{"type": "adult"}] * passengers,
        "cabin_class": "economy"}}).encode()
    return call("duffel", key,
                lambda: get_json(f"{BASE}?return_offers=true",
                                 headers=_headers(), body=payload))


def offers(origin: str, destination: str, day: datetime, after: datetime | None = None):
    """Wire format -> Offer, keeping only departures the traveller can reach."""
    from offers import Offer

    out = []
    for o in search(origin, destination, day).get("data", {}).get("offers", []):
        slice_ = o["slices"][0]
        segments = slice_["segments"]
        first, last = segments[0], segments[-1]

        # Endpoints come from the SLICE, not from a segment. A segment is one
        # leg: on SIN -> ZRH the first is SIN -> MUC, so reading it labelled the
        # offer "SIN to MUC", priced a Munich flight as a Zurich one, and told
        # the engine the traveller would arrive at the wrong airport six hours
        # early. Direct routes hid this; every long-haul is connecting.
        start = slice_.get("origin") or first["origin"]
        end = slice_.get("destination") or last["destination"]
        dep = _aware(first["departing_at"], start["iata_code"], day.tzinfo, start)
        arr = _aware(last["arriving_at"], end["iata_code"], day.tzinfo, end)
        if after and dep < after:
            continue        # already on the ground before the traveller lands
        if arr <= dep:
            continue        # a timezone we could not resolve; drop rather than invent

        conditions = o.get("conditions") or {}
        refund = conditions.get("refund_before_departure") or {}
        change = conditions.get("change_before_departure") or {}
        refundable = bool(refund.get("allowed"))
        change_fee = None
        if change.get("allowed"):
            raw_fee = change.get("penalty_amount")
            # A change that is allowed with no stated penalty is free; one with a
            # penalty in another currency goes through the same conversion as the
            # fare, because a EUR total with a CHF fee inside it is not a total.
            fee = float(raw_fee) if raw_fee not in (None, "") else 0.0
            change_fee = _to_trip_currency(fee, change.get("penalty_currency") or "")[0]

        stops = len(segments) - 1
        num = f"{o['owner']['iata_code']} {first['operating_carrier_flight_number']}"
        price, source, quoted = _to_trip_currency(
            float(o["total_amount"]), o.get("total_currency", ""))
        out.append(Offer(
            # The full id, never a prefix. Duffel ids all start "off_0000",
            # so truncating to twelve characters made every offer look like
            # every other one — and since the booking's ticket group is derived
            # from it, two separately purchased flights became one contract.
            # That is the exact fiction the whole scenario exists to disprove.
            id=o["id"], mode="flight", carrier=o["owner"]["name"],
            label=f"{num} - {start['iata_code']} to {end['iata_code']}"
                  + (f" via {segments[0]['destination']['iata_code']}" if stops == 1
                     else f", {stops} stops" if stops else ""),
            depart=dep, arrive=arr,
            origin=start["iata_code"], destination=end["iata_code"],
            price=price, currency=TRIP_CURRENCY,
            price_source=source, quoted=quoted, book_url="",
            refundable=refundable, change_fee=change_fee))
    return out
