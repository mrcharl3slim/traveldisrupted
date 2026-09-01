"""Things the traveller has to be at, which nobody sold them.

The point of these is that an appointment is not a new kind of thing. It is a
place, a time and a consequence for not being there — which is what every other
row in an itinerary already is — so it becomes a `Booking` and the whole engine
applies to it unchanged. Most of what is tested here is that the engine really
does apply, and that the three things it must never guess are never guessed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import appointment as ap
import flow
from builder import clashes
from domain import Trip
from graph import Severity, propagate

CEST = timezone(timedelta(hours=2))
TODAY = date(2026, 8, 29)
DAY = datetime(2026, 9, 18, 12, 0, tzinfo=CEST)


@pytest.fixture(scope="module")
def trip():
    """Zurich to Milan, landing mid-afternoon, two nights in Milan."""
    flight = min(flow.search_flights("ZRH", "MXP", DAY), key=lambda o: o.arrive)
    stays = flow.search_hotels("Milan", "IT", DAY, DAY + timedelta(days=2),
                               code="MXP")
    built, _ = flow.select([flight], stays[:1])
    return built


def made(text: str) -> ap.Appointment:
    return ap.parse(text, TODAY)


# --------------------------------------------------------------------------
# who, when, where — and nothing invented
# --------------------------------------------------------------------------


def test_the_worked_example():
    a = made("meeting with the Milan team on 19 September at 10am at their office")
    assert a.who == "Milan team"
    assert a.day == date(2026, 9, 19)
    assert a.when.hour == 10
    assert a.where == "their office"
    # Not ready: an address the engine cannot place has no clock, and the last
    # gate is the traveller agreeing it was heard right.
    assert [g.field for g in a.gaps()] == ["place"]
    settled = ap.answer(ap.answer(a, "place", "Milan"), "confirm", "yes")
    assert settled.ready


def test_the_three_questions_are_the_three_that_decide_anything():
    """Who, when and where. The day and time place it against the itinerary,
    the location decides whether it can be reached, and who it is with is what
    makes the answer legible when it is read back."""
    assert {g.field for g in made("i have a call").gaps()} == {"who", "day", "where"}


def test_a_known_day_asks_for_a_time_rather_than_the_day_again():
    a = made("lunch with Priya on 19 sep")
    fields = [g.field for g in a.gaps()]
    assert "day" not in fields and "at" in fields


def test_nothing_about_the_three_is_ever_guessed():
    """A meeting whose time we invented is worse than no meeting at all,
    because it will be checked against flights and pronounced feasible."""
    a = made("meeting with the vendor")
    assert a.when is None and a.day is None and a.where == ""
    assert not a.ready


class Echoes:
    """Verbatim what Haiku returns for "i have a meeting" over bedrock.

    Every key correctly null except the day, which comes back as today. It is
    not being careless: a meeting is on a day, the only day in front of it is
    the one the prompt supplies so that "next Thursday" can be resolved, and
    "Do not infer" loses to that.
    """

    def invoke(self, _prompt):
        return ('{"what": "meeting", "who": null, "where": null, '
                '"day": "2026-08-29", "at": null, "minutes": null}')


def test_a_day_is_not_invented_for_a_sentence_that_names_none():
    """The guess does not merely add a wrong day -- it removes the question
    that would have caught it. `gaps` stops asking "which day", so the
    traveller is asked what time, and when they later type "19 september" the
    additive merge keeps the guess and discards what they said. The meeting is
    then filed on a day nobody chose and checked against flights."""
    base = ap.parse("i have a meeting", TODAY)
    assert base.day is None
    assert ap.enrich(base, Echoes(), TODAY).day is None
    assert [a.field for a in ap.enrich(base, Echoes(), TODAY).gaps()][:2] == [
        "who", "day"]


def test_a_day_the_traveller_did_raise_is_still_read_by_the_model():
    """The guard is corroboration, not a ban. "Next Thursday" is a cue and not
    a date -- `_dates` cannot resolve it and the model can, which is the only
    reason it is in this path."""
    base = ap.parse("meeting with the vendor next thursday", TODAY)
    assert base.day is None, "the pattern settled this and the model is moot"
    assert ap.enrich(base, Echoes(), TODAY).day == TODAY


def test_a_named_part_of_the_day_becomes_a_stated_hour():
    """"Morning" has to become a number before anything can be checked against
    a flight — and the number is shown, not hidden."""
    a = ap.answer(made("meeting with the team on 19 sep"), "at", "morning", TODAY)
    assert a.when and a.when.hour == 9


def test_an_unreadable_answer_leaves_the_slot_open():
    a = made("meeting with the team on 19 sep")
    assert ap.answer(a, "at", "whenever suits", TODAY) == a


def test_a_duration_is_read_when_stated():
    assert made("review with the vendor on 19 sep at 2pm for 2 hours").minutes == 120
    assert made("standup with the team on 19 sep at 9am for 15 mins").minutes == 15
    assert made("call with Sam on 19 sep at 9am").minutes == ap.DEFAULT_MINUTES


def test_the_title_is_composed_when_it_is_read_not_when_it_is_typed():
    """Who it is with is usually answered several turns after the word
    "meeting" was typed. Fixing the title early leaves "Meeting" sitting in a
    clash line that would have read "Meeting with the Milan design team"."""
    a = ap.answer(made("i have a meeting on 19 september"), "who", "the vendor", TODAY)
    assert a.title() == "Meeting with the vendor"


# --------------------------------------------------------------------------
# it is a booking, so the engine already knows what to do with it
# --------------------------------------------------------------------------


def test_an_appointment_becomes_a_booking_with_no_price(trip):
    a = made("meeting with the Milan team on 18 september at 4pm at MXP")
    b = ap.localise(a, trip).to_booking()
    assert b.price == 0.0 and b.commitment and b.fixed_slot
    assert b.where == "MXP"


def test_a_commitment_is_not_filed_under_nothing_prepaid(trip):
    """The most expensive wrong answer this system could give. A EUR 0 dinner
    with free cancellation costs nothing to miss; a EUR 0 meeting with the
    Milan team is the reason the trip exists, and the engine reads value from
    `price` everywhere else."""
    from domain import Disruption

    a = made("meeting with the Milan team on 18 september at 4pm at MXP")
    combined = ap.attach(trip, a)
    leg = [b for b in combined.in_order() if b.kind.value == "flight"][0]
    cancelled = Disruption(leg.id, leg.start, "cancelled", 1.0, cancelled=True)

    impact = propagate(combined, cancelled, leg.start)
    node = impact.by_id([b for b in combined.in_order() if b.commitment][0].id)
    assert node.severity is Severity.BROKEN
    assert "miss" in node.reason


def test_a_missed_commitment_is_counted_and_never_priced(trip):
    """"EUR 445 and you miss the Milan meeting" is two facts a traveller weighs
    differently. Inventing a euro value for the second so it can join the first
    would be the engine making up the most important number on the page."""
    from domain import Disruption

    a = made("meeting with the Milan team on 18 september at 4pm at MXP")
    combined = ap.attach(trip, a)
    leg = [b for b in combined.in_order() if b.kind.value == "flight"][0]
    impact = propagate(combined,
                       Disruption(leg.id, leg.start, "", 1.0, cancelled=True),
                       leg.start)

    assert impact.missed, "a commitment that cannot be attended went unreported"
    assert all(n.exposure == 0.0 for n in impact.missed)
    assert impact.do_nothing_cost == sum(n.exposure for n in impact.broken)


# --------------------------------------------------------------------------
# feasible, and what it collides with
# --------------------------------------------------------------------------


def test_a_meeting_while_you_are_airborne_is_a_clash(trip):
    leg = [b for b in trip.in_order() if b.kind.value == "flight"][0]
    during = leg.start + timedelta(minutes=30)
    a = made(f"meeting with the team on 18 september at "
             f"{during:%-I}:{during:%M}{'am' if during.hour < 12 else 'pm'} at MXP")
    verdict = ap.assess(trip, a)
    assert not verdict["feasible"]
    assert any("overlaps" in c for c in verdict["about_this"])


def test_a_meeting_after_you_land_is_fine(trip):
    a = made("meeting with the team on 18 september at 9pm at MXP")
    assert ap.assess(trip, a)["feasible"]


def test_somewhere_the_engine_cannot_reach_is_said_plainly(trip):
    """`transit` returns None for pairs it does not know, and None means there
    is no ground route — not "assume it is fine"."""
    a = made("workshop with the vendor on 19 september at 10am at ZRH")
    verdict = ap.assess(trip, a)
    assert not verdict["feasible"]
    assert any("no route" in c for c in verdict["about_this"])


def test_two_meetings_at_once_clash_with_each_other(trip):
    first = made("meeting with the vendor on 18 september at 7pm at MXP")
    second = made("call with Priya on 18 september at 7:30pm at MXP")
    both = ap.attach(ap.attach(trip, first), second)
    assert any("overlaps" in c for c in clashes(both))


def test_a_hotel_is_somewhere_you_may_be_not_somewhere_you_must_be(trip):
    """Treating a three-night stay as a three-day commitment makes every
    meeting in the trip a clash."""
    a = made("meeting with the team on 19 september at 11am at MXP")
    verdict = ap.assess(trip, a)
    assert not any("Palace" in c for c in verdict["clashes"])


# --------------------------------------------------------------------------
# which trip
# --------------------------------------------------------------------------


def test_the_trip_is_chosen_by_the_day(trip):
    inside = made("meeting with the team on 19 september at 11am at MXP")
    outside = made("meeting with the team on 30 november at 11am at MXP")
    assert ap.candidates(inside, [("t1", trip)]) == ["t1"]
    assert ap.candidates(outside, [("t1", trip)]) == []


def test_an_unresolved_place_widens_rather_than_filters(trip):
    """"Their office" resolves to nothing, and narrowing on nothing would
    silently exclude the right trip."""
    vague = made("meeting with the team on 19 september at 11am at their office")
    assert vague.place == ""
    assert ap.candidates(vague, [("t1", trip)]) == ["t1"]


def test_a_naive_time_is_put_on_the_trips_clock(trip):
    """Everything in an itinerary is timezone-aware because a Zurich arrival
    rendered in Singapore time is a different flight. Python raises rather than
    quietly comparing the two, so this is settled at the seam."""
    a = made("meeting with the team on 18 september at 4pm at MXP")
    assert a.when.tzinfo is None
    assert ap.localise(a, trip).when.tzinfo is not None


# --------------------------------------------------------------------------
# a trip that crosses timezones
# --------------------------------------------------------------------------

SGT = timezone(timedelta(hours=8))
EDT = timezone(timedelta(hours=-4))


@pytest.fixture
def long_haul():
    """Singapore to New York: departs on the 31st, lands late on the 1st."""
    from domain import Booking, Kind
    from policy import Policy

    return Trip([Booking(
        id="br0226", kind=Kind.FLIGHT, provider="EVA Air",
        title="BR 0226 - SIN to JFK via TPE",
        start=datetime(2026, 8, 31, 23, 30, tzinfo=SGT),
        end=datetime(2026, 9, 1, 22, 5, tzinfo=EDT),
        origin="SIN", destination="JFK", price=1200.0, policy=Policy())])


def test_nine_am_means_nine_am_where_the_meeting_is(long_haul):
    """The bug a traveller hit. A 09:00 meeting in New York, on an itinerary
    starting in Singapore, was stamped 09:00 +08:00 -- which is 21:00 the
    previous evening in New York, twelve hours out, and an hour before the
    flight it was then said to clash with."""
    a = made("meeting with john on 2 september at 9am at JFK")
    when = ap.localise(a, long_haul).when
    assert when.strftime("%d %b %H:%M") == "02 Sep 09:00"
    assert when.utcoffset() == timedelta(hours=-4), "stamped on the wrong clock"


def test_a_meeting_after_a_long_haul_lands_is_not_a_clash(long_haul):
    a = made("meeting with john on 2 september at 9am at JFK")
    verdict = ap.assess(long_haul, a)
    assert verdict["feasible"], verdict["clashes"]


def test_a_known_place_beats_where_the_traveller_happens_to_be(long_haul):
    """Best source first: if the traveller named somewhere the engine knows,
    that place's own clock is the right one."""
    a = made("meeting with the team on 2 september at 9am at ZRH")
    assert ap.zone_for(a, long_haul) is not None
    when = ap.localise(a, long_haul).when
    assert when.utcoffset() == timedelta(hours=2)     # Europe/Zurich in September


def test_a_clash_names_both_dates(long_haul):
    """The message that hid this. A bare time for the meeting next to a full
    date for the flight left no way to see which day the engine had chosen --
    and when it chose wrong, the message concealed it."""
    a = made("meeting with john on 1 september at 9am at JFK")
    hits = ap.assess(long_haul, a)["about_this"]
    assert hits and "01 Sep 09:00" in hits[0]


# --------------------------------------------------------------------------
# somewhere the engine has never heard of
# --------------------------------------------------------------------------


def test_an_address_we_do_not_know_is_not_a_place_we_cannot_reach(long_haul):
    """"No route from JFK to ritz carlton" reads as "you cannot get there" and
    means "I do not know where that is". Turning the second into the first
    invents a clash out of our own ignorance."""
    a = made("meeting with john on 2 september at 9am at ritz carlton")
    verdict = ap.assess(long_haul, a)
    assert verdict["feasible"]
    assert not any("no route" in c for c in verdict["clashes"])
    assert verdict["notes"], "checked nothing and said nothing"
    assert "ritz carlton" in verdict["notes"][0]


def test_two_places_we_do_know_still_report_a_missing_route(trip):
    """The rule that `transit` returning None means the absence of a route
    holds between two places we know. Only the unknown case is exempt."""
    a = made("workshop with the vendor on 19 september at 10am at ZRH")
    verdict = ap.assess(trip, a)
    assert not verdict["feasible"]
    assert any("no route" in c for c in verdict["about_this"])


def test_the_timing_is_still_checked_when_the_place_is_unknown(long_haul):
    """A location we cannot resolve stops the journey check, not the clock."""
    a = made("meeting with john on 1 september at 9am at ritz carlton")
    verdict = ap.assess(long_haul, a)
    assert not verdict["feasible"], "airborne, and nobody said so"
    assert any("overlaps" in c for c in verdict["about_this"])


# --------------------------------------------------------------------------
# the location decides the clock, so it has to be settled
# --------------------------------------------------------------------------


def test_an_address_must_be_placed_in_a_city_before_anything_is_checked():
    """"9am at the Ritz Carlton" means nine in the morning in New York. Until
    the engine knows which city that is, it is guessing a timezone — and it
    guessed wrong once already, by twelve hours."""
    a = made("meeting with john on 2 september at 9am at the ritz carlton")
    assert a.where and not a.place
    ask = next(g for g in a.gaps() if g.field == "place")
    assert "which city" in ask.question.lower()
    assert "ritz carlton" in ask.question.lower()

    placed = ap.answer(a, "place", "new york", TODAY)
    assert placed.place == "JFK"
    assert "place" not in {g.field for g in placed.gaps()}


def test_a_city_the_engine_does_not_know_leaves_the_question_open():
    a = made("meeting with john on 2 september at 9am at the ritz carlton")
    assert ap.answer(a, "place", "Narnia", TODAY) == a


# --------------------------------------------------------------------------
# confirming what was recorded
# --------------------------------------------------------------------------


def test_nothing_is_filed_until_the_traveller_agrees_it_was_heard_right():
    a = made("meeting with john on 2 september at 9am at MXP")
    assert not a.ready
    assert [g.field for g in a.gaps()] == ["confirm"]
    assert ap.answer(a, "confirm", "yes, save it", TODAY).ready


def test_the_card_says_which_city_the_time_is_in():
    """"09:00" is not a fact until somebody says where. A traveller reading
    "09:00, New York time" catches in one second the mistake that otherwise
    surfaces as a clash with the wrong flight."""
    a = ap.answer(made("meeting with john on 2 september at 9am at the ritz carlton"),
                  "place", "new york", TODAY)
    when = next(r for r in a.card() if r["label"] == "When")
    assert "09:00" in when["value"] and when["note"] == "New York time"

    where = next(r for r in a.card() if r["label"] == "Where")
    assert where["value"] == "ritz carlton" and "New York" in where["note"]


def test_anything_other_than_yes_is_not_a_confirmation():
    a = ap.answer(made("meeting with john on 2 september at 9am at MXP"),
                  "confirm", "no, let me change it", TODAY)
    assert not a.confirmed and not a.ready


import appointment

# -- the presence timeline ------------------------------------------------

def _presence_trip():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from domain import Booking, Kind, Trip
    sgt, cet = ZoneInfo("Asia/Singapore"), ZoneInfo("Europe/Zurich")
    out = Booking(id="out", kind=Kind.FLIGHT, provider="SQ",
                  title="SQ 346 - SIN to ZRH",
                  start=datetime(2026, 9, 18, 1, 25, tzinfo=sgt),
                  end=datetime(2026, 9, 18, 8, 15, tzinfo=cet),
                  origin="SIN", destination="ZRH", price=1000.0)
    back = Booking(id="back", kind=Kind.FLIGHT, provider="SQ",
                   title="SQ 345 - ZRH to SIN",
                   start=datetime(2026, 9, 21, 11, 0, tzinfo=cet),
                   end=datetime(2026, 9, 22, 6, 0, tzinfo=sgt),
                   origin="ZRH", destination="SIN", price=1000.0)
    return Trip([out, back])


def test_a_meeting_in_the_wrong_city_carries_where_you_actually_are():
    """On the 19th the timeline says Zurich; a Singapore meeting that day is
    invalid, and the verdict names the city so the reply can."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    appt = appointment.Appointment(
        what="meeting", who="the board", where="Singapore", place="SIN",
        day=date(2026, 9, 19),
        when=datetime(2026, 9, 19, 10, 0, tzinfo=ZoneInfo("Asia/Singapore")),
        confirmed=True)
    verdict = appointment.assess(_presence_trip(), appt, home="SIN")
    assert verdict["invalid"], "the timeline proves they are in Zurich"
    assert "Zurich" in verdict["invalid"][0]["message"]


def test_less_than_two_hours_after_landing_is_tight_not_infeasible():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    cet = ZoneInfo("Europe/Zurich")
    appt = appointment.Appointment(
        what="meeting", who="a client", where="Zurich", place="ZRH",
        day=date(2026, 9, 18),
        when=datetime(2026, 9, 18, 9, 30, tzinfo=cet), confirmed=True)
    verdict = appointment.assess(_presence_trip(), appt, home="SIN")
    assert not verdict["invalid"]
    assert verdict["tight"] and verdict["tight"][0]["gap_minutes"] == 75
    assert verdict["feasible"], "tight is a warning, not a refusal"


def test_the_two_hour_rule_is_between_appointments_too():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    cet = ZoneInfo("Europe/Zurich")
    first = appointment.Appointment(
        what="meeting", who="ops", where="Zurich", place="ZRH",
        day=date(2026, 9, 19),
        when=datetime(2026, 9, 19, 10, 0, tzinfo=cet), confirmed=True)
    trip = appointment.attach(_presence_trip(), first)
    second = appointment.Appointment(
        what="coffee", who="a friend", where="Zurich", place="ZRH",
        day=date(2026, 9, 19),
        when=datetime(2026, 9, 19, 12, 30, tzinfo=cet), confirmed=True)
    verdict = appointment.assess(trip, second, home="SIN")
    assert verdict["tight"], "11:00 to 12:30 is ninety minutes"
    assert not verdict["about_this"], "a gap is not an overlap"


def test_an_open_ended_trip_lets_a_late_meeting_at_the_last_city_through():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from domain import Trip
    cet = ZoneInfo("Europe/Zurich")
    one_way = Trip(list(_presence_trip().bookings)[:1])
    appt = appointment.Appointment(
        what="review", who="the team", where="Zurich", place="ZRH",
        day=date(2026, 10, 2),
        when=datetime(2026, 10, 2, 15, 0, tzinfo=cet), confirmed=True)
    verdict = appointment.assess(one_way, appt, home="SIN")
    assert not verdict["invalid"], "presumed still in Zurich — said, not hidden"
    assert verdict["presence"]["open_ended"] is True


def test_resaving_the_same_meeting_keeps_one_row():
    """The id survives a storage round trip, so attach() replaces instead of
    appending. A hash-based id never matched its reloaded self."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    import ingest
    from domain import Trip
    cet = ZoneInfo("Europe/Zurich")
    appt = appointment.Appointment(
        what="meeting", who="the client", where="Zurich", place="ZRH",
        day=date(2026, 9, 19),
        when=datetime(2026, 9, 19, 14, 0, tzinfo=cet), confirmed=True)
    once = appointment.attach(_presence_trip(), appt)
    stored, _p, _s = ingest.to_bookings({"bookings": ingest.to_dicts(once.bookings)})
    twice = appointment.attach(Trip(stored), appt)
    assert sum(1 for b in twice.bookings if b.commitment) == 1
