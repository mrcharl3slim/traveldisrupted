"""Reading a sentence, and knowing what is still missing.

The tests that matter here are not "does it parse" -- they are "does it refuse
to guess". An agent that fills a slot to avoid asking a question has made a
booking decision on somebody else's behalf, which is the failure this whole
product exists to prevent, committed by the product itself.
"""

from __future__ import annotations

from datetime import date

import pytest

import converse
import places
import request as rq

TODAY = date(2026, 8, 29)


def parse(text: str) -> rq.Request:
    return rq.parse(text, TODAY)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def test_the_worked_example():
    r = parse("i want to book a flight from singapore to london from 1 to 7 september")
    assert (r.origin, r.destination) == ("SIN", "LHR")
    assert r.depart == date(2026, 9, 1) and r.ret == date(2026, 9, 7)
    assert r.one_way is False


def test_a_month_with_no_year_means_the_next_one():
    """"1 September" said in October is next September. Guessing backwards
    searches a date in the past, which every provider answers with silence."""
    assert rq.parse("fly to london on 1 march", date(2026, 8, 29)).depart \
        == date(2027, 3, 1)
    assert rq.parse("fly to london on 1 december", date(2026, 8, 29)).depart \
        == date(2026, 12, 1)


def test_one_way_beats_a_range_of_days():
    """"london 3-9 nov one way" names a shape and a window to travel in, not a
    return. Reading it as a return books a leg nobody asked for."""
    r = parse("singapore to london 3-9 nov one way")
    assert r.one_way is True and r.ret is None


def test_stated_preferences_are_read_not_invented():
    assert parse("cheapest flight to london").preference == "cheapest"
    assert parse("non-stop to london").preference == "direct"
    assert parse("quickest way to london").preference == "fastest"
    assert parse("flight to london").preference == ""      # nothing said


def test_a_refusal_to_book_a_hotel_is_not_the_same_as_silence():
    """Three states, not two. Nobody has said (ask), said no (don't search),
    said yes (search) -- and collapsing the first two into a default is how an
    agent quietly decides you did not want a room."""
    assert parse("flight to london").hotel is None
    assert parse("flight to london, no hotel").hotel is False
    assert parse("flight to london with a hotel").hotel is True


def test_an_unknown_place_is_left_empty():
    """Never a guess. 'I don't know that airport, which did you mean?' beats a
    confident search of the wrong city."""
    assert parse("fly me to narnia next tuesday").destination == ""
    assert places.find("narnia") is None


def test_the_longest_name_wins():
    """Scanning for 'york' before 'new york' finds another continent."""
    assert parse("singapore to new york on 4 october").destination == "JFK"


# --------------------------------------------------------------------------
# what gets asked
# --------------------------------------------------------------------------


def test_only_questions_that_change_the_answer_are_asked():
    """Every Ask has to earn its place. Seat preference changes nothing this
    system does, so it is not asked however conversational it would sound."""
    fields = {a.field for a in parse("flight somewhere").gaps()}
    assert fields <= {"destination", "origin", "depart", "ret",
                      "hotel", "preference", "hotel_area"}


def test_every_question_says_why_it_is_being_asked():
    for ask in parse("flight to london").gaps():
        assert ask.why, f"{ask.field} asks without saying why"


def test_an_optional_question_never_blocks_a_search():
    r = parse("cheapest flight singapore to london on 3 october one way with a hotel")
    assert r.ready
    assert [a.field for a in r.gaps()] == ["hotel_area"]
    assert all(a.optional for a in r.gaps())


def test_an_unreadable_answer_leaves_the_slot_open():
    """A silently dropped answer is a question the traveller believes they have
    already dealt with."""
    r = parse("flight to london")
    assert rq.answer(r, "depart", "sometime nice", TODAY).depart is None
    assert "depart" in {a.field for a in
                        rq.answer(r, "depart", "sometime nice", TODAY).gaps()}


def test_one_place_and_no_direction_can_be_corrected():
    """'london 3-9 nov' files London as an origin because there is no 'to'.
    Answering 'destination: london' has to move it, not produce LHR to LHR."""
    r = parse("london 3-9 nov")
    r = rq.answer(r, "destination", "london", TODAY)
    assert (r.origin, r.destination) == ("", "LHR")


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------


def test_the_graph_asks_before_it_searches():
    s = converse.turn("flight from singapore to zurich on 18 september", today=TODAY)
    assert s["reply"] in {a.question for a in s["asks"]}
    assert not s.get("ports"), "searched before it had what it needed"


def test_a_follow_up_adds_rather_than_replaces():
    """'actually make it the 3rd' must not wipe the destination."""
    first = converse.turn("flight from singapore to zurich", today=TODAY)["req"]
    second = converse.turn("on 18 september", first, today=TODAY)["req"]
    assert second.destination == "ZRH" and second.depart == date(2026, 9, 18)


def test_the_providers_it_picks_follow_from_the_request():
    r = parse("one way flight zurich to milan on 18 september, cheapest")
    assert converse.route({"req": r})["ports"] == ["duffel"]
    r = rq.answer(r, "hotel", "yes", TODAY)
    assert "liteapi" in converse.route({"req": r})["ports"]


def test_a_forwarded_confirmation_is_told_apart_from_a_request():
    """By shape, not by asking a model. Getting it wrong in the safe direction
    asks a question, which a human corrects in one word."""
    assert converse.classify({"text": "book me a flight to london"})["kind"] == "book"
    email = ("Your booking is confirmed\nBooking reference: XY12AB\n"
             "SQ 346 Singapore to Zurich\nTotal paid: SGD 1,140\n"
             "Fare rules: non-refundable, check-in opens 48h before\n" * 3)
    assert converse.classify({"text": email})["kind"] == "confirmation"


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


class _Offer:
    def __init__(self, price, minutes, label):
        from datetime import datetime, timedelta
        self.price = price
        self.depart = datetime(2026, 9, 18, 9, 0)
        self.arrive = self.depart + timedelta(minutes=minutes)
        self.label = label


def test_direct_is_a_preference_and_not_a_filter():
    """A traveller who wants a direct flight and is shown nothing because none
    exists has been failed by the software, not by the airlines."""
    offers = [_OFF := _Offer(75, 200, "VY 1 - ZRH to MXP via BCN"),
              _Offer(700, 55, "LH 2 - ZRH to MXP")]
    ordered = converse.rank(offers, "direct")
    assert ordered[0].label.endswith("to MXP")     # direct first
    assert len(ordered) == len(offers)             # and nothing dropped


def test_each_preference_orders_by_the_thing_it_names():
    offers = [_Offer(700, 55, "LH 2 - ZRH to MXP"),
              _Offer(75, 200, "VY 1 - ZRH to MXP via BCN")]
    assert converse.rank(offers, "cheapest")[0].price == 75
    assert converse.rank(offers, "fastest")[0].price == 700
    assert converse.rank(offers, "")[0].price == 75          # cheapest by default


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["two way", "coming back", "yes",
                                   "round trip", "returning"])
def test_saying_you_are_coming_back_is_an_answer(reply):
    """The bug this exists to stop. "Coming back, or one way?" asks two things
    -- whether there is a return and when it is -- and every one of these
    answers settles the first without saying anything about the second. They
    were all treated as unreadable, so the agent asked the same question again,
    and again, to somebody who had already answered it."""
    r = rq.parse("flight from singapore to london on 1 september", TODAY)
    after = rq.answer(r, "ret", reply, TODAY)
    assert after.one_way is False, f"{reply!r} was not understood"
    assert after != r


def test_the_follow_up_asks_the_part_that_is_still_missing():
    """A different question, because the first one has been answered."""
    r = rq.parse("flight from singapore to london on 1 september", TODAY)
    r = rq.answer(r, "ret", "coming back", TODAY)
    ask = r.gaps()[0]
    assert ask.field == "ret_date"
    assert "which day" in ask.question.lower()

    r = rq.answer(r, "ret_date", "7 september", TODAY)
    assert r.ret == date(2026, 9, 7)
    assert "ret_date" not in {a.field for a in r.gaps()}


def test_one_way_still_settles_it_outright():
    r = rq.parse("flight from singapore to london on 1 september", TODAY)
    for reply in ("one way", "no"):
        after = rq.answer(r, "ret", reply, TODAY)
        assert after.one_way is True and after.ret is None
        assert "ret" not in {a.field for a in after.gaps()}
        assert "ret_date" not in {a.field for a in after.gaps()}


def test_a_date_answers_both_halves_at_once():
    r = rq.parse("flight from singapore to london on 1 september", TODAY)
    after = rq.answer(r, "ret", "7 september", TODAY)
    assert after.ret == date(2026, 9, 7) and after.one_way is False
    assert not [a for a in after.gaps() if a.field.startswith("ret")]


def test_both_answers_are_offered_as_chips():
    """A question with two answers and one button is a question that expects
    you to type the other one."""
    r = rq.parse("flight from singapore to london on 1 september", TODAY)
    ask = next(a for a in r.gaps() if a.field == "ret")
    assert set(ask.options) == {"coming back", "one way"}


def test_no_question_can_repeat_without_saying_so():
    """An answer that changed nothing was not understood. Asking again with no
    acknowledgement leaves the traveller guessing which word was the problem."""
    r = rq.parse("flight from singapore to london on 1 september", TODAY)
    stuck = rq.answer(r, "ret", "sometime probably", TODAY)
    assert stuck == r, "an unreadable answer must not be silently stored"


def test_a_yes_no_slot_has_three_outcomes():
    """Yes, no, and neither. A slot that treats everything-that-is-not-yes as
    no stored "banana" as "no hotel" and told nobody -- a decision the
    traveller did not make, made silently, which is the exact failure this
    product exists to catch other people committing."""
    r = rq.parse("one way flight zurich to milan on 18 september", TODAY)
    assert rq.answer(r, "hotel", "yes please", TODAY).hotel is True
    assert rq.answer(r, "hotel", "not this time", TODAY).hotel is False
    assert rq.answer(r, "hotel", "banana", TODAY).hotel is None
    assert rq.answer(r, "hotel", "banana", TODAY) == r, "stored a non-answer"


def test_bookkeeping_is_not_mistaken_for_an_answer():
    """`raw` changes on every turn because it holds whatever was last typed.
    Comparing whole Requests to ask 'did that tell me anything?' answers yes to
    'banana', and the acknowledgement never fires."""
    r = rq.parse("one way flight zurich to milan on 18 september", TODAY)
    from dataclasses import replace as _replace
    assert _replace(r, raw="something else").settled == r.settled
    assert _replace(r, preference="direct").settled != r.settled
