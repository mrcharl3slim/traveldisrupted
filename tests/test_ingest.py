"""Turning confirmations into a Trip.

The engine has always been trustworthy; its input has not existed. Everything
here is about the failure modes of extraction, because a wrong booking produces
confident arithmetic about a trip nobody is taking — which is worse than no
answer at all.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import ingest
from demo_trip import build_trip
from domain import Booking, Kind

SGT, CEST = timezone(timedelta(hours=8)), timezone(timedelta(hours=2))


def _raw(**over):
    booking = {
        "kind": "flight", "provider": "Singapore Airlines",
        "title": "SQ 346 - Singapore to Zurich",
        "start": "2026-10-11T23:55:00+08:00", "end": "2026-10-12T06:35:00+02:00",
        "origin": "SIN", "destination": "ZRH", "price": 1140, "currency": "SGD",
        "ticket_group": "SQ-9F2K", "fare_rules": "Economy Value - change fee S$150",
        "source": "Departs 11 Oct 23:55 SIN, arrives 12 Oct 06:35 ZRH",
    }
    booking.update(over)
    return {"bookings": [booking], "problems": []}


# -- what must never be guessed ------------------------------------------

def test_a_timestamp_without_a_timezone_is_dropped():
    """The failure this rule exists for is silent: a naive time becomes the
    server's timezone, which is a different flight."""
    bookings, problems, _ = ingest.to_bookings(_raw(start="2026-10-11T23:55:00"))
    assert bookings == []
    assert problems and "timezone" in problems[0]


def test_a_booking_with_no_title_is_dropped_not_named():
    bookings, problems, _ = ingest.to_bookings(_raw(title=""))
    assert bookings == []
    assert problems


def test_missing_fare_rules_mean_non_refundable_not_a_guess():
    """The engine reads an empty Policy as "you cannot get this back". Failing
    that way under-promises; inventing a refund costs a traveller money."""
    bookings, _, _ = ingest.to_bookings(_raw(fare_rules=None))
    assert bookings[0].policy.source == ""
    assert not bookings[0].policy.windows


def test_the_booking_reference_is_carried_verbatim():
    """Two bookings on one reference are one contract and the airline owes a
    connection; two references are two contracts and nobody owes anything. That
    distinction is the entire scenario, so it must survive extraction."""
    bookings, _, _ = ingest.to_bookings(_raw())
    assert bookings[0].ticket_group == "SQ-9F2K"


def test_every_booking_keeps_the_line_it_came_from():
    _bookings, _problems, sources = ingest.to_bookings(_raw())
    assert any("23:55" in s for s in sources.values())


def test_an_unknown_kind_does_not_become_a_flight():
    """Deadline rules are written per kind. An unknown kind quietly inheriting
    flight boarding buffers would move a deadline nobody asked to move."""
    bookings, _, _ = ingest.to_bookings(_raw(kind="pottery class"))
    assert bookings[0].kind is ingest.DEFAULT_KIND
    assert bookings[0].kind is not Kind.FLIGHT


# -- physics --------------------------------------------------------------

def test_an_unreachable_connection_is_reported_not_corrected():
    raw = _raw()
    raw["bookings"].append({
        "kind": "flight", "provider": "SWISS", "title": "LX 1608 - Zurich to Milan",
        "start": "2026-10-12T06:50:00+02:00", "end": "2026-10-12T08:05:00+02:00",
        "origin": "ZRH", "destination": "MXP", "price": 142, "currency": "EUR",
        "ticket_group": "LX-4T81", "source": "LX1608 06:50"})
    bookings, _, _ = ingest.to_bookings(raw)
    warnings = ingest.validate(bookings)
    assert warnings and "unreachable" in warnings[0]
    # and the times are untouched
    assert bookings[1].start.hour == 6


def test_a_booking_that_ends_before_it_starts_is_flagged():
    bookings, _, _ = ingest.to_bookings(_raw(end="2026-10-11T20:00:00+08:00"))
    assert any("ends before" in w for w in ingest.validate(bookings))


def test_bookings_come_back_in_chronological_order():
    raw = _raw()
    raw["bookings"].insert(0, {
        "kind": "lodging", "provider": "Hotel", "title": "Later stay",
        "start": "2026-10-14T14:00:00+02:00", "source": "check-in 14:00"})
    bookings, _, _ = ingest.to_bookings(raw)
    assert [b.start for b in bookings] == sorted(b.start for b in bookings)


# -- without a model ------------------------------------------------------

def test_no_model_extracts_nothing_and_says_so():
    """A regex reaches a flight number and not a fare rule, a reference or a
    timezone. Half a trip is not a cheaper trip; it is a wrong one."""
    result = ingest.extract("Your flight SQ346 departs tomorrow at 11pm")
    assert result.trip.bookings == []
    assert result.problems and "no model" in result.problems[0]
    assert not result.ok


# -- with a model ---------------------------------------------------------

class _Reply:
    def __init__(self, content): self.content = content


class _Model:
    """Stands in for Bedrock. Records what it was asked."""

    def __init__(self, content): self.content, self.calls = content, []

    def invoke(self, messages):
        self.calls.append(messages)
        return _Reply(self.content)


class _DeadModel:
    def invoke(self, messages):
        raise RuntimeError("bedrock: ThrottlingException")


def test_extraction_runs_the_model_when_there_is_one():
    """The test that was missing, and the reason a 500 shipped.

    Every other extract test passes no model, so `if model else None` short
    circuits and the call into `_model` is never made. That is exactly the
    branch the deployed instance takes -- render.yaml sets LLM_PROVIDER=bedrock
    -- and `_model.ask_json` did not exist, so pasting a confirmation raised
    AttributeError through an unguarded call. Green suite, dead headline flow.
    """
    model = _Model(json.dumps({"bookings": [_raw()["bookings"][0]], "problems": []}))
    result = ingest.extract("Your Singapore Airlines confirmation", model=model)
    assert model.calls, "the model was never asked"
    assert [b.id for b in result.trip.bookings]
    assert result.trip.bookings[0].origin == "SIN"


def test_a_model_that_fails_costs_extraction_not_the_request():
    """Throttling, an expired token, a region without the model enabled. All
    of them mean a templated answer, none of them mean a 500."""
    result = ingest.extract("Your flight SQ346 departs tomorrow", model=_DeadModel())
    assert result.trip.bookings == []
    assert result.problems and "no model" in result.problems[0]


def test_prose_around_the_json_is_survivable():
    """Models fence their output however firmly you ask them not to."""
    body = "Certainly.\n```json\n" + json.dumps(
        {"bookings": [_raw()["bookings"][0]], "problems": []}) + "\n```"
    result = ingest.extract("confirmation", model=_Model(body))
    assert len(result.trip.bookings) == 1


def test_a_model_answering_with_nothing_usable_falls_back():
    result = ingest.extract("Your flight SQ346 departs tomorrow",
                            model=_Model("I could not read that."))
    assert result.trip.bookings == []
    assert not result.ok


# -- the round trip -------------------------------------------------------

def test_every_booking_field_survives_storage_or_is_named_as_dropped():
    """The protection that matters.

    Two earlier versions of the serialiser enumerated fields by hand and missed
    some — first the resolved fare windows, then hard_deadline and mitigation.
    Each time the engine did not fail; it answered confidently with the wrong
    number (EUR 906 instead of EUR 282, because a hotel lost the guarantee that
    it would hold the room). A field added to Booking tomorrow must break this
    test rather than a total six weeks from now.
    """
    stored = set(ingest.to_dicts(build_trip(None).in_order())[0])
    stored.add("fare_rules")          # the serialised name for policy.source
    stored.add("windows")             # and for policy.windows
    fields = {f.name for f in dataclasses.fields(Booking)}
    missing = fields - stored - ingest.NOT_STORED
    assert not missing, f"Booking fields lost in storage: {sorted(missing)}"


def test_a_stored_trip_reproduces_the_original_exactly():
    original = build_trip(None).in_order()
    restored, problems, _ = ingest.to_bookings({"bookings": ingest.to_dicts(original)})
    assert not problems
    assert len(restored) == len(original)
    for before, after in zip(original, restored):
        assert (after.title, after.start, after.end) == (before.title, before.start, before.end)
        assert after.ticket_group == before.ticket_group
        assert after.hard_deadline == before.hard_deadline
        assert after.mitigation == before.mitigation
        assert after.must_arrive_by == before.must_arrive_by
        assert [w.closes for w in after.policy.windows] == [w.closes for w in before.policy.windows]
