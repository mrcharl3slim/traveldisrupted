"""The ports must parse what the providers actually send.

These run in replay against committed fixtures, so they need no keys, no
network and no luck -- which is the same property that makes the demo safe.
"""

from datetime import datetime, timedelta

import pytest

import aerodatabox
import duffel
import rail
from base import MODE, PortError
from demo_trip import CEST, dt
from plan import generate
from demo_trip import TRIP

DAY = dt(12, 9, 0)
STATIONS = {"Zürich HB": "ZRH_HB", "Milano Centrale": "MILANO_C"}


def test_replay_is_the_default():
    """Nobody should have to remember to turn safety on."""
    assert MODE == "replay"


def test_rail_returns_the_eurocity():
    offers = rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS)
    ec = next(o for o in offers if o.depart == dt(12, 11, 33))
    assert ec.arrive == dt(12, 15, 20)
    assert ec.destination == "MILANO_C"


def test_rail_drops_connections_with_transfers():
    """A hero plan should not hinge on a same-day change in a foreign station
    for somebody who has been awake fourteen hours."""
    offers = rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS)
    assert all(o.depart != dt(12, 12, 0) for o in offers)
    assert len(offers) == 2


def test_the_rail_fare_is_labelled_an_estimate():
    """transport.opendata.ch sells timetables, not prices. The number under
    Plan B's headline is the one figure the engine cannot quote."""
    o = rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS)[0]
    assert o.price_source == "estimate"
    assert o.book_url


def test_duffel_quotes_real_fares():
    offers = duffel.offers("ZRH", "MXP", DAY)
    prices = {o.price for o in offers}
    assert {118.0, 89.0} <= prices
    assert all(o.price_source == "quoted" for o in offers)


def test_duffel_discards_departures_before_the_traveller_lands():
    """LX 1604 leaves at 07:05. The traveller is airborne until 10:25."""
    offers = duffel.offers("ZRH", "MXP", DAY, after=dt(12, 10, 25))
    assert all(o.depart >= dt(12, 10, 25) for o in offers)
    assert "1604" not in " ".join(o.label for o in offers)


def test_status_becomes_a_disruption():
    d = aerodatabox.disruption("SQ346", dt(11, 9, 0), "sq346")
    assert d.new_end == dt(12, 10, 25)
    assert d.delay(TRIP) == timedelta(hours=3, minutes=50)
    assert d.confidence == 0.91


def test_live_offers_reproduce_the_ranking():
    """The whole point of the port layer: swapping seeded data for parsed API
    responses must not move a single number."""
    d = aerodatabox.disruption("SQ346", dt(11, 9, 0), "sq346")
    now = datetime(2026, 10, 12, 2, 38, tzinfo=CEST)
    live = (duffel.offers("ZRH", "MXP", DAY, after=d.new_end)
            + rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS))
    plans = generate(TRIP, d, now, live)
    best = plans[0]
    assert best.arrives_at == dt(12, 15, 20)
    assert best.net_cash == -14.0
    assert best.total_damage == 176.0
    assert plans[-1].id == "noop" and plans[-1].total_damage == 282.0


def test_a_missing_recording_fails_loudly():
    with pytest.raises(PortError, match="record.py"):
        rail.connections("Bern", "Napoli", DAY)
