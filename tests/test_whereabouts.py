"""The traveller is somewhere at every moment, and the itinerary says where.

Pure tests over hand-built trips: home before departure, transit during a
leg, the destination until the next leg, home after the return -- and the two
honesty rules: unknown places never refuse anything, and a missing return leg
is a presumption said out loud, not a silent belief.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import whereabouts
from domain import Booking, Kind, Trip

SGT = ZoneInfo("Asia/Singapore")
CET = ZoneInfo("Europe/Zurich")


def _flight(bid, origin, dest, start, end):
    return Booking(id=bid, kind=Kind.FLIGHT, provider="XX",
                   title=f"{bid} - {origin} to {dest}",
                   start=start, end=end, origin=origin, destination=dest,
                   price=100.0)


OUT = _flight("out", "SIN", "ZRH",
              datetime(2026, 9, 18, 1, 25, tzinfo=SGT),
              datetime(2026, 9, 18, 8, 15, tzinfo=CET))
BACK = _flight("back", "ZRH", "SIN",
               datetime(2026, 9, 21, 11, 0, tzinfo=CET),
               datetime(2026, 9, 22, 6, 0, tzinfo=SGT))
HOTEL = Booking(id="stay", kind=Kind.LODGING, provider="H", title="A hotel",
                start=datetime(2026, 9, 18, 14, 0, tzinfo=CET),
                end=datetime(2026, 9, 21, 10, 0, tzinfo=CET),
                origin="ZRH", price=300.0)

ROUND = Trip([OUT, HOTEL, BACK])


def test_home_before_departure_and_transit_during_the_leg():
    tl = whereabouts.timeline(ROUND, home="SIN")
    assert tl[0].city == "Singapore" and tl[0].frm is None
    assert tl[0].to == OUT.start
    before = whereabouts.at(tl, datetime(2026, 9, 17, 12, 0, tzinfo=SGT))
    assert before.city == "Singapore" and before.source == "home"
    airborne = whereabouts.at(tl, datetime(2026, 9, 18, 4, 0, tzinfo=SGT))
    assert airborne.source == "transit" and airborne.city == ""


def test_the_destination_holds_until_the_next_leg():
    tl = whereabouts.timeline(ROUND, home="SIN")
    mid = whereabouts.at(tl, datetime(2026, 9, 19, 15, 0, tzinfo=CET))
    assert mid.city == "Zurich" and mid.source == "out"
    assert mid.to == BACK.start


def test_home_again_after_the_return_leg():
    tl = whereabouts.timeline(ROUND, home="SIN")
    after = whereabouts.at(tl, datetime(2026, 9, 25, 9, 0, tzinfo=SGT))
    assert after.city == "Singapore" and after.to is None
    assert not whereabouts.open_ended(tl, home="SIN")


def test_no_return_leg_is_open_ended_and_presumes_the_last_city():
    one_way = Trip([OUT, HOTEL])
    tl = whereabouts.timeline(one_way, home="SIN")
    assert whereabouts.open_ended(tl, home="SIN")
    later = whereabouts.at(tl, datetime(2026, 10, 1, 9, 0, tzinfo=CET))
    assert later.city == "Zurich" and later.to is None


def test_no_home_derives_presence_from_the_first_legs_origin():
    tl = whereabouts.timeline(Trip([OUT]), home="")
    assert tl[0].city == "Singapore", "they must be there to depart"


def test_no_transport_legs_means_home_only_or_nothing():
    lone = Trip([HOTEL])
    assert whereabouts.timeline(lone, home="SIN")[0].city == "Singapore"
    assert whereabouts.timeline(lone, home="") == []


def test_hotels_and_appointments_never_move_the_traveller():
    """origin-only bookings are places to be, not vehicles."""
    meeting = Booking(id="mtg", kind=Kind.ACTIVITY, provider="who",
                      title="Meeting", commitment=True, fixed_slot=True,
                      start=datetime(2026, 9, 19, 10, 0, tzinfo=CET),
                      end=datetime(2026, 9, 19, 11, 0, tzinfo=CET),
                      origin="ZRH", price=0.0)
    tl = whereabouts.timeline(Trip([OUT, HOTEL, meeting, BACK]), home="SIN")
    assert [iv.source for iv in tl if iv.source not in ("home", "transit")] \
        == ["out", "back"]


def test_station_and_airport_codes_are_the_same_city():
    """Arriving at MXP and meeting near MILANO C.le is one city, not two."""
    hop = _flight("hop", "ZRH", "MXP",
                  datetime(2026, 9, 18, 9, 40, tzinfo=CET),
                  datetime(2026, 9, 18, 10, 35, tzinfo=CET))
    tl = whereabouts.timeline(Trip([OUT, hop]), home="SIN")
    span = datetime(2026, 9, 18, 14, 0, tzinfo=CET)
    assert whereabouts.valid_here(tl, whereabouts._city("MILANO_C"),
                                  span, span + timedelta(hours=1))


def test_the_wrong_city_is_invalid_and_an_unknown_one_is_not():
    tl = whereabouts.timeline(ROUND, home="SIN")
    day19 = datetime(2026, 9, 19, 10, 0, tzinfo=CET)
    assert not whereabouts.valid_here(tl, "Singapore", day19,
                                      day19 + timedelta(hours=1))
    assert not whereabouts.valid_here(tl, "Tokyo", day19,
                                      day19 + timedelta(hours=1))
    # Unknown city "" -> no opinion, never a refusal
    assert whereabouts.valid_here(tl, "", day19, day19 + timedelta(hours=1))


def test_an_overnight_leg_spans_midnight_as_transit():
    tl = whereabouts.timeline(Trip([BACK]), home="")
    midnight = datetime(2026, 9, 21, 23, 0, tzinfo=CET)
    assert whereabouts.at(tl, midnight).source == "transit"


def test_same_day_multi_leg_yields_each_city_in_turn():
    hop = _flight("hop", "ZRH", "MXP",
                  datetime(2026, 9, 18, 9, 40, tzinfo=CET),
                  datetime(2026, 9, 18, 10, 35, tzinfo=CET))
    tl = whereabouts.timeline(Trip([OUT, hop]), home="SIN")
    cities = [iv.city for iv in tl if iv.city]
    assert cities == ["Singapore", "Zurich", "Milan"]


def test_less_than_two_hours_is_tight_in_the_same_city_only():
    meeting = Booking(id="mtg", kind=Kind.ACTIVITY, provider="who",
                      title="Meeting", commitment=True, fixed_slot=True,
                      start=datetime(2026, 9, 18, 9, 30, tzinfo=CET),
                      end=datetime(2026, 9, 18, 10, 30, tzinfo=CET),
                      origin="ZRH", price=0.0)
    trip = Trip([OUT, meeting])
    found = whereabouts.tight(trip, meeting)
    assert found and found[0]["gap_minutes"] == 75
    assert "after" in found[0]["message"]

    far = Booking(id="mtg2", kind=Kind.ACTIVITY, provider="who",
                  title="Milan call", commitment=True, fixed_slot=True,
                  start=datetime(2026, 9, 18, 9, 30, tzinfo=CET),
                  end=datetime(2026, 9, 18, 10, 30, tzinfo=CET),
                  origin="MXP", price=0.0)
    assert whereabouts.tight(Trip([OUT, far]), far) == [], \
        "a different city is the wrong-city rule's business, not the gap's"


def test_the_gap_before_a_flight_runs_to_check_in_not_wheels_up():
    meeting = Booking(id="mtg", kind=Kind.ACTIVITY, provider="who",
                      title="Meeting", commitment=True, fixed_slot=True,
                      start=datetime(2026, 9, 21, 8, 0, tzinfo=CET),
                      end=datetime(2026, 9, 21, 9, 0, tzinfo=CET),
                      origin="ZRH", price=0.0)
    found = whereabouts.tight(Trip([BACK, meeting]), meeting)
    # BACK departs 11:00, must_arrive_by 10:30; 9:00 -> 90 min, under 2h
    assert found and found[0]["gap_minutes"] == 90
    assert "before" in found[0]["message"]
