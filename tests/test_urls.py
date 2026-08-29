"""The URLs the ports build — the one thing replay cannot check.

In replay, `call` returns the recording and never invokes the lambda that
builds a URL. So every port shipped its own interpolation, none of them were
ever executed by a test, and the first city with a space in it took the live
demo down:

    InvalidURL: URL can't contain control characters.
    '/v3.0/data/hotels?countryCode=US&cityName=New York&limit=5'

These tests reach past `call` and build the URL directly, which is the only way
this class of bug is visible without a network. Every port that takes a place
name from a traveller is here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import aerodatabox
import hotels
import rail
from base import url_for

CEST = timezone(timedelta(hours=2))
WHEN = datetime(2026, 9, 18, 9, 0, tzinfo=CEST)

#: Every one of these appears in a real place name, and each fails differently
#: against raw interpolation. The space raises; the rest quietly query for
#: something else, which is worse.
AWKWARD = ["New York", "Zürich HB", "Washington D.C.", "Kuala Lumpur",
           "Baden-Baden", "L'Aquila", "São Paulo", "A & B"]


def _valid(url: str) -> None:
    """A URL urllib will accept, with nothing raw left in it."""
    assert " " not in url, url
    assert all(ord(c) > 31 and ord(c) < 127 for c in url), url
    parsed = urlparse(url)
    assert parsed.scheme in ("http", "https") and parsed.netloc, url


@pytest.mark.parametrize("city", AWKWARD)
def test_a_hotel_search_survives_a_real_city_name(city):
    """The crash, as a test. LiteAPI takes the city as a query parameter and
    the port put it there raw."""
    url = url_for(f"{hotels.BASE}/data/hotels",
                  countryCode="US", cityName=city, limit=5)
    _valid(url)
    assert parse_qs(urlparse(url).query)["cityName"] == [city]


@pytest.mark.parametrize("station", AWKWARD)
def test_a_rail_search_survives_more_than_a_space(station):
    """Rail hand-rolled `.replace(' ', '%20')`, which survives a space and
    mangles every accent — a half-fix that looks like a fix, and the more
    dangerous kind because it does not raise."""
    url = url_for(rail.BASE, **{"from": station, "to": "Milano Centrale",
                                "date": "2026-09-18", "time": "09:00",
                                "limit": 12})
    _valid(url)
    assert parse_qs(urlparse(url).query)["from"] == [station]


def test_a_flight_number_loses_its_space_rather_than_encoding_it():
    """"SQ 346" and "SQ346" are the same flight and this API wants the second.
    Stripping is right here and encoding would be wrong — which is exactly why
    it belongs in one reviewed place rather than in three ports."""
    url = url_for(f"https://{aerodatabox.HOST}",
                  f"flights/number/{'SQ 346'.replace(' ', '')}/2026-09-18")
    _valid(url)
    assert url.endswith("/flights/number/SQ346/2026-09-18")


def test_empty_parameters_are_left_out_not_sent_blank():
    """A blank `cityName=` is a different query from no `cityName` at all, and
    providers disagree about which one they answer."""
    url = url_for("https://x.example", cityName="", limit=5, extra=None)
    assert "cityName" not in url and "extra" not in url
    assert url.endswith("?limit=5")


def test_a_path_segment_is_quoted_too():
    """A city reaches a path as easily as it reaches a query."""
    url = url_for("https://x.example", "cities/New York/hotels")
    _valid(url)
    assert "New%20York" in url


def test_every_port_that_takes_a_place_name_builds_a_valid_url(monkeypatch):
    """The regression guard with the widest reach: drive each port past `call`
    with an awkward name and assert on the URL it would actually request.

    Without this, the next port added interpolates its own query string, and
    nothing notices until the next live demo.
    """
    seen: list[str] = []

    def capture(url, headers=None, body=None, timeout=None):
        seen.append(url)
        return {}

    monkeypatch.setattr("base.MODE", "live")
    for module in (hotels, rail, aerodatabox):
        monkeypatch.setattr(module, "get_json", capture)

    hotels.catalogue("New York", "US", limit=5)
    rail.connections("Zürich HB", "Milano Centrale", WHEN)
    aerodatabox.status("SQ 346", WHEN)

    assert len(seen) == 3
    for url in seen:
        _valid(url)
