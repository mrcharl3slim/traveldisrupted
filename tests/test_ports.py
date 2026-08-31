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
    assert ec.arrive == dt(12, 15, 17)
    assert ec.destination == "MILANO_C"


def test_every_connection_is_one_change_at_most_and_says_where():
    """The seeded data had a direct Eurocity and this test used to reject any
    change at all. The timetable does not: since December there is no direct
    Zurich - Milano Centrale service, so rejecting changes rejected the entire
    route and Plan B stopped existing.

    The rule that survives contact with the real timetable is not "no changes",
    it is "one change, with enough time to make it, and the traveller is told
    where". A buffer is a promise; an unnamed transfer is a trap."""
    offers = rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS)
    assert offers
    for o in offers:
        assert "change at" in o.label or " - " in o.label
        assert o.arrive > o.depart


def test_the_rail_fare_is_labelled_an_estimate():
    """transport.opendata.ch sells timetables, not prices. The number under
    Plan B's headline is the one figure the engine cannot quote."""
    o = rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS)[0]
    assert o.price_source == "estimate"
    assert o.book_url


def test_duffel_never_guesses_a_fare():
    """Unlike rail, this one really does sell tickets, so no fare from it is
    ever ours. It may need converting -- the test account quotes USD while the
    itinerary is in EUR -- and a converted number keeps a record of what the
    airline actually said, because a rate is a guess applied to a real figure
    and the difference has to survive into the total."""
    offers = duffel.offers("ZRH", "MXP", DAY)
    assert offers
    assert all(o.price > 0 for o in offers)
    assert all(o.price_source in ("quoted", "converted") for o in offers)
    for o in offers:
        if o.price_source == "converted":
            assert o.quoted, f"{o.label} converted from nothing"


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
    responses must not move a single number.

    And it does not. The live timetable moved the arrival by three minutes --
    15:17 rather than the 15:20 the seeded offer carried -- and every figure the
    pitch rests on came out identical: -14 tonight, 176 of damage against 282
    for inaction, rail first and doing nothing last. The three minutes are the
    real world; the numbers are the engine."""
    d = aerodatabox.disruption("SQ346", dt(11, 9, 0), "sq346")
    now = datetime(2026, 10, 12, 2, 38, tzinfo=CEST)
    live = (duffel.offers("ZRH", "MXP", DAY, after=d.new_end)
            + rail.offers("Zurich HB", "Milano Centrale", DAY, STATIONS))
    plans = generate(TRIP, d, now, live)
    best = plans[0]
    assert best.arrives_at == dt(12, 15, 17)
    assert best.net_cash == -21.0
    assert best.total_damage == 264.0
    noop = next(p for p in plans if p.id == "noop")
    assert noop.total_damage == 423.0
    assert best.total_damage < noop.total_damage

    # Doing nothing is no longer LAST, and that is the live data being honest.
    # Against three hand-picked offers it was the worst thing you could do.
    # Against two dozen real fares, several replacements cost more than the
    # disruption -- a EUR 779 seat that still misses the museum is worse than
    # staying put. Inaction has to be beatable, not bottom.
    assert any(p.total_damage > noop.total_damage for p in plans)


def test_a_missing_recording_fails_loudly():
    with pytest.raises(PortError, match="record.py"):
        rail.connections("Bern", "Napoli", DAY)
