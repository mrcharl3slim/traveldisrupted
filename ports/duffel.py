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

# Duffel timestamps are local wall-clock with NO offset -- "2026-10-12T15:30:00"
# means half past three in Zurich, and Python will happily compare that to an
# aware datetime by raising. The itinerary is entirely tz-aware, so the zone has
# to be reattached here, at the boundary, rather than leaking naive datetimes
# into the engine. Only the airports this demo touches; a real deployment reads
# the zone from the airport reference data Duffel already returns.
AIRPORT_TZ = {"ZRH": 2, "MXP": 2, "SIN": 8, "FLR": 2}


def _aware(stamp: str, iata: str, fallback):
    t = datetime.fromisoformat(stamp)
    if t.tzinfo is not None:
        return t
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
        seg = o["slices"][0]["segments"][0]
        dep = _aware(seg["departing_at"], seg["origin"]["iata_code"], day.tzinfo)
        arr = _aware(seg["arriving_at"], seg["destination"]["iata_code"], day.tzinfo)
        if after and dep < after:
            continue        # already on the ground before the traveller lands
        carrier = o["owner"]["name"]
        num = f"{o['owner']['iata_code']} {seg['operating_carrier_flight_number']}"
        out.append(Offer(
            id=o["id"][:12], mode="flight", carrier=carrier,
            label=f"{num} - {seg['origin']['iata_code']} to {seg['destination']['iata_code']}",
            depart=dep, arrive=arr,
            origin=seg["origin"]["iata_code"], destination=seg["destination"]["iata_code"],
            price=float(o["total_amount"]), currency=o["total_currency"],
            price_source="quoted", book_url=""))
    return out
