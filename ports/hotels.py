"""Hotels, via LiteAPI.

WHY NOT DUFFEL STAYS. One vendor and one token would be tidier, but Duffel's
test hotels exist only at latitude -24.38, longitude -128.32 -- a fixed point in
the Pacific. A demo about missing a connection to Milan cannot put the hotel in
the middle of the ocean, so this costs one more key and keeps the trip coherent.

Two calls, because LiteAPI splits them: find the hotels in a city, then ask for
rates on specific ones for specific dates. The split is why `stays` takes a
limit -- rates are priced per hotel and asking for two hundred of them to show
five is rude to somebody else's API.

Sandbox keys return real hotels in real cities and cannot take money.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta

from base import call, get_json

BASE = "https://api.liteapi.travel/v3.0"

#: When a hotel day starts and when the room stops being held. Neither is in the
#: rate response -- they are product knowledge, and the second one matters: it
#: is the deadline that makes a late arrival a phone call rather than a loss.
CHECK_IN = time(14, 0)
ARRIVAL_GUARANTEE = time(22, 0)


def _headers() -> dict[str, str]:
    return {"X-API-Key": os.environ.get("LITEAPI_KEY", ""),
            "Accept": "application/json",
            "Content-Type": "application/json"}


def catalogue(city: str, country: str, limit: int = 10) -> dict:
    """Hotels in a city. Reference data — slow-moving, worth recording once."""
    key = {"city": city, "country": country, "limit": str(limit)}
    url = (f"{BASE}/data/hotels?countryCode={country}&cityName={city}"
           f"&limit={limit}")
    return call("hotels", key, lambda: get_json(url, headers=_headers()))


def rates(hotel_ids: list[str], checkin: datetime, checkout: datetime,
          adults: int = 1, currency: str = "EUR") -> dict:
    """Priced availability for specific hotels on specific nights."""
    key = {"hotels": ",".join(sorted(hotel_ids))[:60],
           "in": f"{checkin:%Y-%m-%d}", "out": f"{checkout:%Y-%m-%d}"}
    payload = json.dumps({
        "hotelIds": hotel_ids,
        "checkin": f"{checkin:%Y-%m-%d}",
        "checkout": f"{checkout:%Y-%m-%d}",
        "occupancies": [{"adults": adults}],
        "currency": currency,
        "guestNationality": "SG",
    }).encode()
    return call("rates", key,
                lambda: get_json(f"{BASE}/hotels/rates", headers=_headers(),
                                 body=payload))


def _first_price(entry: dict) -> tuple[float, str] | None:
    """Dig the cheapest quoted total out of a rates entry.

    Written to tolerate shape drift: providers move totals between `retailRate`,
    `offerRetailRate` and per-room arrays between versions, and a KeyError here
    would take out the whole search rather than one hotel.
    """
    for rooms in (entry.get("roomTypes") or [entry]):
        rate = (rooms.get("offerRetailRate") or rooms.get("retailRate")
                or rooms.get("rate") or {})
        if isinstance(rate, dict) and rate.get("amount") is not None:
            return float(rate["amount"]), str(rate.get("currency") or "EUR")
        for candidate in (rooms.get("rates") or []):
            inner = candidate.get("retailRate", {}).get("total") or []
            if inner:
                return float(inner[0]["amount"]), str(inner[0].get("currency") or "EUR")
    return None


def stays(city: str, country: str, checkin: datetime, checkout: datetime,
          tz, limit: int = 5) -> list[dict]:
    """City + dates -> priced stays, ready to become Bookings.

    ``tz`` is required rather than inferred: a hotel's check-in time is local to
    the hotel, and a naive timestamp entering the engine is the failure that
    silently moves a deadline into another country.
    """
    listing = catalogue(city, country, limit=limit)
    hotels = listing.get("data") or listing.get("hotels") or []
    if not hotels:
        return []

    by_id = {str(h.get("id") or h.get("hotelId")): h for h in hotels if h}
    priced = rates(list(by_id)[:limit], checkin, checkout)

    out: list[dict] = []
    for entry in (priced.get("data") or []):
        hotel_id = str(entry.get("hotelId") or entry.get("id") or "")
        hotel = by_id.get(hotel_id, {})
        money = _first_price(entry)
        if money is None:
            continue
        amount, currency = money
        out.append({
            "id": f"hotel-{hotel_id}"[:20],
            "provider": "LiteAPI",
            "name": hotel.get("name") or f"Hotel {hotel_id}",
            "city": hotel.get("city") or city,
            "address": hotel.get("address") or "",
            "stars": hotel.get("stars") or hotel.get("starRating"),
            "check_in": datetime.combine(checkin.date(), CHECK_IN, tzinfo=tz),
            "check_out": datetime.combine(checkout.date(), CHECK_IN, tzinfo=tz),
            "arrival_guarantee": datetime.combine(
                checkin.date(), ARRIVAL_GUARANTEE, tzinfo=tz),
            "nights": max(1, (checkout.date() - checkin.date()).days),
            "price": amount,
            "currency": currency,
        })
    return sorted(out, key=lambda s: s["price"])
