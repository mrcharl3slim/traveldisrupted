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


# -- China, by air only ---------------------------------------------------

def test_the_chinese_cities_are_searchable_by_name_and_by_code():
    """A place is searchable when the table knows it, and not before: before
    this, "shanghai" stopped the conversation with "I do not know a place
    called that" while Duffel would have sold the flight."""
    import places

    for said, code in (("shanghai", "PVG"), ("beijing", "PEK"),
                       ("peking", "PEK"), ("guangzhou", "CAN"),
                       ("shenzhen", "SZX"), ("chengdu", "CTU"),
                       ("xi an", "XIY"), ("urumqi", "URC"), ("PVG", "PVG")):
        found = places.find(said)
        assert found and found.code == code, said


def test_every_chinese_city_is_on_one_clock():
    """Mainland China runs a single timezone, Urumqi included. Guessing a
    zone per city is how a 09:00 meeting lands twelve hours out; there is
    nothing to guess here, and this says so out loud."""
    import places

    zones = {p.zone for p in places.PLACES if p.country == "CN"}
    assert zones == {"Asia/Shanghai"}, zones


def test_a_chinese_station_is_a_promise_a_port_can_keep():
    """A station in the table means `search_rail` has somewhere to ask. It
    was empty while the only rail API here was the Swiss timetable; now a
    Chinese pair routes to the China port instead of coming back empty and
    reading as a fault rather than an absence."""
    import chinarail
    import flow
    import places

    named = [p for p in places.PLACES if p.country == "CN" and p.station_code]
    assert named, "the cities that have a main station say so"
    assert flow._railway(places.by_code("PVG"), places.by_code("PEK")) is chinarail
    assert flow._railway(places.by_code("ZRH"), places.by_code("MXP")) is not chinarail
    assert flow._railway(places.by_code("ZRH"), places.by_code("PVG")) is None, (
        "no single railway runs Zurich to Shanghai, and pretending otherwise "
        "is worse than an empty list")


def test_every_chinese_station_can_be_reached_from_its_airport():
    """An airport and its own city's station are separate islands without a
    ground link, so the engine would not offer a train to somebody who has
    just landed -- the quiet failure this table exists to prevent."""
    import places
    from domain import transit

    for p in places.PLACES:
        if p.country == "CN" and p.station_code:
            assert transit(p.code, p.station_code) is not None, p.code


def test_the_china_port_speaks_the_shape_every_rail_port_speaks():
    """One function is the whole contract, which is what makes swapping 12306
    for a partner API a change to one file."""
    import inspect

    import chinarail
    import rail

    assert (list(inspect.signature(chinarail.offers).parameters)
            == list(inspect.signature(rail.offers).parameters))
    assert chinarail.ESTIMATE > 0
    assert "12306" in chinarail.DEEP_LINK


from datetime import timedelta  # noqa: E402


def test_english_station_names_reach_the_pinyin_12306_files_them_under():
    """Chinese stations are named by direction -- there are five main stations
    in Beijing and the direction IS the name -- and 12306 keeps it in pinyin:
    "beijingnan", never "beijing south". Without the compass every readable
    label in places.py resolved to nothing and each search refused a station
    plainly in the list."""
    import chinarail

    listed = {"beijingnan": "VNP", "shanghaihongqiao": "AOH",
              "xianbei": "EAY", "wuhan": "WHN"}
    assert chinarail._telecode(listed, "Beijing South") == "VNP"
    assert chinarail._telecode(listed, "Shanghai Hongqiao") == "AOH"
    assert chinarail._telecode(listed, "Xi'an North") == "EAY"
    assert chinarail._telecode(listed, "Wuhan") == "WHN"
    assert chinarail._telecode(listed, "Nowhere Central") is None, (
        "a station it cannot resolve is a refusal, never a guess")


def test_a_train_row_is_validated_rather_than_trusted():
    """The response packs each train into a pipe-delimited string and this
    port reads positions nobody promised to keep stable. A row that does not
    look like a train is dropped here rather than becoming a confident wrong
    answer three modules away."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import chinarail

    day = datetime(2026, 9, 26, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    parts = [""] * 14
    parts[3], parts[8], parts[9] = "G1234", "08:00", "13:30"
    parts[10], parts[13] = "05:30", "20260926"
    code, depart, arrive = chinarail._row("|".join(parts), day)
    assert code == "G1234"
    assert (depart.hour, depart.minute) == (8, 0)
    assert arrive - depart == timedelta(hours=5, minutes=30)
    assert depart.tzinfo == day.tzinfo, "local to the platform it leaves from"

    parts[3] = "NOT-A-TRAIN"
    assert chinarail._row("|".join(parts), day) is None
    assert chinarail._row("too|short", day) is None
