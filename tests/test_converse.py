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
    r = parse("cheapest flight singapore to london on 3 october one way "
              "with a hotel for 4 nights")
    assert [a.field for a in r.gaps()] == ["confirm", "hotel_area"]
    agreed = rq.answer(r, "confirm", "yes", TODAY)
    assert agreed.ready
    assert all(a.optional for a in agreed.gaps())


def test_the_room_gets_its_own_dates_rather_than_the_flights():
    """Arriving on the 18th does not mean checking in on the 18th: a red-eye
    lands at 06:00 and the room is wanted from the night before, and a
    traveller staying with family for two nights wants three of the five.
    Deriving it from the flights is right often enough to be trusted and wrong
    quietly."""
    r = parse("singapore to london 1 to 7 september with a hotel")
    ask = next(a for a in r.gaps() if a.field == "hotel_dates")
    assert "01 Sep – 07 Sep (the whole trip)" in ask.options, ask.options
    assert not r.ready, "booked a room nobody chose the nights for"

    whole = rq.answer(r, "hotel_dates", "the whole trip", TODAY)
    assert (whole.check_in, whole.check_out) == (date(2026, 9, 1), date(2026, 9, 7))
    assert whole.nights == 6

    part = rq.answer(r, "hotel_dates", "3 to 6 september", TODAY)
    assert (part.check_in, part.check_out) == (date(2026, 9, 3), date(2026, 9, 6))
    assert part.nights == 3


def test_half_an_answer_about_the_room_is_not_stored():
    """A single date is half an answer, and half an answer stored is a
    checkout somebody never chose."""
    r = parse("singapore to london 1 to 7 september with a hotel")
    assert rq.answer(r, "hotel_dates", "the 3rd", TODAY) == r


def test_nights_still_work_when_stated_outright():
    r = parse("one way singapore to london on 1 september, hotel for 5 nights")
    assert (r.check_in, r.check_out) == (date(2026, 9, 1), date(2026, 9, 6))
    assert "hotel_dates" not in {a.field for a in r.gaps()}


def test_saying_you_are_coming_back_in_the_opening_sentence_is_heard():
    """Previously ignored, so the agent asked "coming back, or one way?" of
    somebody who had just told it. A question you have already been answered is
    the fastest way to look like a form."""
    r = parse("flight singapore to zurich 18 september coming back")
    assert r.one_way is False
    fields = [a.field for a in r.gaps()]
    assert "ret" not in fields and fields[0] == "ret_date"


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
    # Rail joins it unasked wherever there is a station at each end. Not a
    # question: whether a train runs is the timetable's answer, and "train or
    # plane?" put to somebody flying Singapore to Bangkok is a question about
    # something that does not exist.
    assert converse.route({"req": r})["ports"] == ["duffel", "rail"]
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


def test_rail_is_asked_only_where_there_is_a_station_at_each_end():
    """And never asked ABOUT. `converse` asks what changes the answer, and
    whether a train runs between two cities is the timetable's answer rather
    than the traveller's preference -- so "train or plane?" put to somebody
    flying Singapore to Bangkok is a question about something that does not
    exist. Where trains run they appear in the same list; where they do not,
    nothing is asked and nothing is shown."""
    milan = parse("one way zurich to milan on 18 september, cheapest")
    assert "rail" in converse.route({"req": milan})["ports"]

    bangkok = parse("one way singapore to bangkok on 18 september, cheapest")
    assert "rail" not in converse.route({"req": bangkok})["ports"]
    assert not any(a.field == "mode" for a in bangkok.gaps()), "asked anyway"


def test_a_train_that_changes_is_not_a_direct_service():
    """"Direct" ranked every connecting train above every direct flight, which
    is the preference honoured backwards. The rail label says where the change
    is in its own words rather than in the airline's."""
    class Leg:
        def __init__(self, label):
            self.label = label

    assert converse.stops(Leg("EC 09:33 - Zürich HB to Milano Centrale")) == 0
    assert converse.stops(
        Leg("EC 09:33 - Zürich HB to Milano Centrale, change at Chiasso (36 min)")) == 1
    assert converse.stops(Leg("LH 1628 - ZRH to MXP")) == 0
    assert converse.stops(Leg("JU 0333 - ZRH to MXP via BEG")) == 1


class Echoes:
    """A model that answers about the whole request when shown a fragment.

    Not a strawman: this is verbatim what Haiku returns for these two inputs
    through `LLM_PROVIDER=bedrock`. It is being reasonable -- a flight leaves
    on a day, the only day it has been shown is the one in the prompt -- which
    is exactly why the prompt cannot be the thing that stops it.
    """

    def __init__(self, payload: str):
        self.payload = payload

    def invoke(self, _prompt):
        return self.payload


ONE_WAY = ('{"origin_city": null, "destination_city": null, '
           '"depart": "2026-08-29", "ret": null, "travellers": null, '
           '"hotel": false, "preference": null}')
TWO_NIGHTS = ('{"origin_city": null, "destination_city": null, '
              '"depart": null, "ret": "2026-08-31", "travellers": null, '
              '"hotel": true, "preference": null}')


def test_a_room_is_not_declined_on_the_travellers_behalf():
    """The whole product in one boolean. A hallucinated date at least goes on
    the card to be checked; `hotel: false` does not appear anywhere -- it
    deletes the question, and nothing asks again. Answering "one way" left the
    traveller with no room and no memory of having refused one."""
    base = rq.parse("one way", TODAY)
    assert base.hotel is None
    assert rq.enrich(base, Echoes(ONE_WAY), TODAY).hotel is None

    # Still allowed where the traveller did raise it, in words the pattern
    # does not cover: "nights" is not in the deterministic list, and reading
    # it is the whole reason there is a model in this path at all.
    nights = rq.parse("one way, three nights in milan", TODAY)
    assert nights.hotel is None, "the pattern settled this and the model is moot"
    assert rq.enrich(nights, Echoes(TWO_NIGHTS), TODAY).hotel is True


def test_a_date_is_not_taken_from_a_sentence_that_names_no_day():
    """The prompt names today so "next Tuesday" can be resolved, which also
    hands the model a date to reach for when there is none. Two words about a
    return leg came back with today as the departure."""
    base = rq.parse("one way", TODAY)
    assert rq.enrich(base, Echoes(ONE_WAY), TODAY).depart is None

    cued = rq.parse("leaving tomorrow", TODAY)
    assert rq.enrich(cued, Echoes(ONE_WAY), TODAY).depart == TODAY, (
        "a cue the traveller put there is what the model is for")


def test_one_way_is_not_undone_by_a_date_nobody_asked_for():
    """`one_way` was set to False as a side effect of accepting a return date,
    so a settled answer was overwritten by a blank being filled -- and "one
    way" followed by any sentence with a number in it grew a return leg."""
    base = rq.answer(rq.parse("zurich to milan 18 september", TODAY),
                     "ret", "one way", TODAY)
    assert base.one_way is True

    after = rq.enrich(base, Echoes(TWO_NIGHTS), TODAY)
    assert after.one_way is True and after.ret is None


def test_a_return_before_the_outbound_is_not_a_return():
    """Arithmetic, where a cue cannot help: "2 nights" genuinely refers to a
    span, so it passes any test for whether a day was mentioned. What comes
    back is the anchor date with two nights added to it, which lands three
    weeks before the flight out."""
    base = rq.answer(rq.parse("zurich to milan 18 september", TODAY),
                     "ret", "coming back", TODAY)
    assert base.one_way is False and base.ret is None
    assert rq.enrich(base, Echoes(TWO_NIGHTS), TODAY).ret is None


def test_a_model_that_cannot_start_is_reported_not_hidden(monkeypatch):
    """/health must not report an intention as a fact. A provider configured
    but not working reads identically to a working one otherwise, for as long
    as nobody looks closely at the phrasing."""
    import _model

    monkeypatch.setattr(_model, "PROVIDER", "bedrock")
    monkeypatch.setattr(_model, "unavailable", "")
    monkeypatch.setattr(_model, "_CACHE", {})
    monkeypatch.setattr(_model, "_build",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no access")))

    status = _model.effective()
    assert status["configured"] == "bedrock"
    assert status["ready"] is False
    assert "no access" in status["why_not"]


def test_choosing_no_model_is_not_reported_as_a_fault(monkeypatch):
    """"none" is a supported mode. Flagging it as broken would train everyone
    to ignore the one line that matters when something really is broken."""
    import _model

    monkeypatch.setattr(_model, "PROVIDER", "none")
    monkeypatch.setattr(_model, "unavailable", "")
    monkeypatch.setattr(_model, "_CACHE", {})
    assert _model.effective()["why_not"] == ""


def test_a_model_call_that_fails_is_recorded(monkeypatch):
    """The model is allowed to fail and the booking is allowed to continue.
    Both. What is not allowed is failing quietly."""
    import _model

    class Broken:
        def invoke(self, _prompt):
            raise RuntimeError("expired token")

    monkeypatch.setattr(_model, "PROVIDER", "bedrock")
    monkeypatch.setattr(_model, "unavailable", "")
    base = rq.parse("take me somewhere", TODAY)
    assert rq.enrich(base, Broken(), TODAY) == base      # carried on
    assert "expired token" in _model.unavailable         # and said so


# --------------------------------------------------------------------------
# confirming what was recorded
# --------------------------------------------------------------------------


def test_nothing_is_searched_until_it_has_been_agreed():
    """Dates and airports are the two things a sentence gets wrong most often
    and the two a traveller can check in a second — and every fare and every
    deadline after this point is worked out from them."""
    r = rq.answer(parse("zurich to milan 18 to 20 september with a hotel, "
                        "cheapest"), "hotel_dates", "the whole trip", TODAY)
    assert not r.ready
    assert "confirm" in {a.field for a in r.gaps()}
    assert rq.answer(r, "confirm", "yes, search it", TODAY).ready


def test_the_card_spells_the_dates_out_in_full():
    """"18/09" and "09/18" are the same six characters and different days."""
    r = rq.answer(parse("zurich to milan 18 to 20 september with a hotel, "
                        "cheapest"), "hotel_dates", "the whole trip", TODAY)
    rows = {row["label"]: row for row in r.card()}
    assert rows["From date"]["value"] == "Friday 18 September"
    assert rows["To date"]["value"] == "Sunday 20 September"
    assert rows["From where"]["note"] == "ZRH" and rows["To where"]["note"] == "MXP"
    assert rows["Hotel"]["value"] == "18 Sep – 20 Sep · 2 nights"


def _at_the_card(text: str):
    """The state a traveller is in once the summary is on screen.

    Built by walking the same path the product walks, because the thing that
    puts the card up is also the thing that records that it went up. Hand-
    assembling the request instead would test a state the product never
    reaches.
    """
    req = converse.turn(text, today=TODAY)["req"]
    req = rq.answer(req, "hotel_dates", "the whole trip", TODAY)
    req = converse.turn("", req, today=TODAY)["req"]        # the card goes up
    assert [a.field for a in req.gaps() if not a.optional] == ["confirm"]
    assert req.shown, "the card was asked for and not recorded as shown"
    return req


def test_a_correction_at_the_confirmation_overwrites():
    """Collecting is additive so "make it the 3rd" cannot wipe the
    destination. Correcting is not: once everything is settled and the
    traveller is looking at "have I got this right?", the only reason to type
    is to change something, and an additive merge would ignore them."""
    settled = _at_the_card("zurich to milan 18 to 20 september with a hotel, "
                           "cheapest")
    fixed = converse.turn("actually the 19th to the 22nd", settled,
                          today=TODAY)["req"]
    assert fixed.depart == date(2026, 9, 19) and fixed.ret == date(2026, 9, 22)
    assert fixed.destination == "MXP", "a correction wiped something it did not mention"
    assert not fixed.confirmed, "a correction left the old confirmation standing"


def test_collecting_is_still_additive():
    """The rule that protects a half-finished request has not been traded away
    to get corrections working."""
    first = converse.turn("flight from singapore to zurich", today=TODAY)["req"]
    second = converse.turn("on 18 september", first, today=TODAY)["req"]
    assert second.destination == "ZRH" and second.depart == date(2026, 9, 18)


def test_blocking_questions_come_before_optional_ones():
    """"Anywhere in particular to stay?" is a strange chip to offer under
    "have I got this right?"."""
    r = rq.answer(parse("zurich to milan 18 to 20 september with a hotel, "
                        "cheapest"), "hotel_dates", "the whole trip", TODAY)
    fields = [a.field for a in r.gaps()]
    assert fields[0] == "confirm"
    assert all(a.optional for a in r.gaps()[1:])


def test_a_bare_day_is_read_against_the_month_on_the_card():
    """"actually the 19th to the 22nd" is how a person corrects a date they
    are looking at. The month is the one already recorded."""
    settled = _at_the_card("zurich to milan 18 to 20 september with a hotel, "
                           "cheapest")
    fixed = converse.turn("actually the 19th to the 22nd", settled,
                          today=TODAY)["req"]
    assert (fixed.depart, fixed.ret) == (date(2026, 9, 19), date(2026, 9, 22))


def test_a_bare_day_answers_the_return_question():
    """"the 22nd" is a whole answer to "which day are you coming back?".

    Asking the question named the month, so the ordinal does not need one --
    and a day that has already gone by the time the trip leaves belongs to the
    month after. A return before the departure is not a date worth storing.
    """
    out = rq.answer(parse("zurich to milan on 18 september"),
                    "ret_date", "the 22nd", TODAY)
    assert out.ret == date(2026, 9, 22)

    late = rq.answer(parse("zurich to milan on 30 september"),
                     "ret_date", "the 3rd", TODAY)
    assert late.ret == date(2026, 10, 3), "the return was filed before the flight out"


def test_a_bare_number_is_not_mistaken_for_a_date():
    """"2 adults" and "2 nights" are far commoner than "the 2nd", and reading
    them as a date would be the confident kind of wrong. Only an ordinal or a
    leading "the" counts."""
    settled = _at_the_card("zurich to milan 18 to 20 september with a hotel, "
                           "cheapest")
    same = converse.turn("2 adults", settled, today=TODAY)["req"]
    assert same.depart == date(2026, 9, 18)
    assert same.travellers == 2


def test_declining_the_confirmation_is_an_answer_not_a_failure():
    """The worst line in the product: tapping the system's own
    "no, let me change it" button and being told "Sorry, I couldn't make that
    out." It settles nothing, so the question stays open — but it is
    understood, and saying otherwise calls the traveller's own button
    gibberish."""
    assert rq.declined("no, let me change it")
    assert rq.declined("nope")
    assert rq.declined("not quite")
    assert rq.declined("change the dates")
    assert not rq.declined("yes, search it")
    assert not rq.declined("")
