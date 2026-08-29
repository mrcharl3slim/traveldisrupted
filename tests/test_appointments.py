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
    assert a.ready


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
