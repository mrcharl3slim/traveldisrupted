"""The paper trail: assembly of four fields that already existed, verified.

The acceptance is the deck's own: an end-to-end scripted run writes one entry
per decision, every entry carries a non-empty citation and authorization
state, and no entry can be written without a timestamp. The last one is
structural -- the Event refuses to construct -- and the test proves the
refusal rather than trusting it.
"""

from __future__ import annotations

import pytest

# fastapi is a HARD requirement (see requirements.txt), not an optional extra,
# so this is a plain import. It was `pytest.importorskip("fastapi")`, and with
# fastapi absent from a virtualenv the entire HTTP surface -- every route, the
# whole capability model, the audit trail -- reduced to one skip line in a run
# that printed "268 passed" and read as healthy. Two ship-blocking bugs lived
# behind that green: an identity hole and a 500 on the headline flow. A missing
# dependency must now fail loudly.
import fastapi  # noqa: F401
from fastapi.testclient import TestClient          # noqa: E402

import audit                                        # noqa: E402
import serve                                        # noqa: E402
import store as store_module                        # noqa: E402


@pytest.fixture(scope="module")
def told():
    """The scripted run: booked, met, shared, delayed, cancelled, answered."""
    client = TestClient(serve.app)
    # A browser with no identity owns nothing -- see roles.role_of. The page
    # asks for one before it does anything, and so does the suite: a fixture
    # that stayed anonymous was, until this line, silently sharing one identity
    # with every other anonymous visitor, which is precisely the hole these
    # tests are meant to be guarding.
    who = client.post("/api/people", json={"name": "the tester"}).json()
    client.headers.update({"Authorization": f"Bearer {who['token']}"})
    story = client.post("/api/story").json()
    return client, story, store_module.store().trail(story["trip_id"])


def of(events, kind):
    return [e for e in events if e["type"] == kind]


# --------------------------------------------------------------------------
# the acceptance, verbatim
# --------------------------------------------------------------------------


def test_one_entry_per_decision(told):
    _client, _story, events = told
    # One extraction: the trip entered the store once.
    assert len(of(events, audit.EXTRACTED)) == 1
    # Two disruptions answered -- the 30-minute delay and the cancellation --
    # so two assessments and two judgements, however many candidate plans
    # propagate was run over on the way.
    assert len(of(events, audit.ASSESSED)) == 2
    assert len(of(events, audit.JUDGED)) == 2
    # The agent took the cancellation's plan: one performed entry per action.
    performed = of(events, audit.PERFORMED)
    assert len(performed) >= 3, "the taken plan had at least notify+buy+monitor"


def test_every_entry_explains_itself(told):
    _client, _story, events = told
    assert events, "a scripted run that decided things wrote nothing"
    for e in events:
        for field in ("at", "actor", "type", "subject",
                      "citation", "authorization", "feed"):
            assert str(e.get(field, "")).strip(), f"{e['type']} entry missing {field}"


def test_no_entry_can_be_written_without_a_timestamp():
    with pytest.raises(ValueError):
        audit.Event(at="", actor="x", type="y", subject="z",
                    citation="c", authorization="a", feed="f")
    with pytest.raises(ValueError):
        audit.Event(at="now", actor="x", type="y", subject="z",
                    citation="  ", authorization="a", feed="f")


def test_timestamps_are_stamped_by_the_module_and_ordered(told):
    _client, _story, events = told
    stamps = [e["at"] for e in events]
    assert stamps == sorted(stamps), "an append-only trail reads in order"


# --------------------------------------------------------------------------
# the four fields land where the deck says they come from
# --------------------------------------------------------------------------


def test_the_judgement_quotes_the_verdict_and_the_rationale(told):
    _client, _story, events = told
    cancelled = of(events, audit.JUDGED)[-1]
    assert "within your S$300 limit" in cancelled["authorization"]
    assert "keeps Meeting with the client" in cancelled["citation"]


def test_the_assessment_names_the_feed_and_the_mode(told):
    _client, _story, events = told
    for e in of(events, audit.ASSESSED):
        assert "(replay)" in e["feed"] or "(live)" in e["feed"] or "no feed" in e["feed"]
        assert "must_arrive_by" in e["citation"], "the rule, not a vibe"


def test_the_performed_entries_carry_lane_and_outcome(told):
    _client, _story, events = told
    states = {e["subject"].split("→")[-1].split(" via")[0].strip()
              for e in of(events, audit.PERFORMED)}
    assert "sent" in states and "pending" in states, (
        "what ran and what waits are both on the record")


def test_the_extraction_cites_the_fare_rules(told):
    _client, _story, events = told
    extracted = of(events, audit.EXTRACTED)[0]
    assert "refundable" in extracted["citation"].lower() or \
           "rate conditions" in extracted["citation"].lower()


# --------------------------------------------------------------------------
# who may read it
# --------------------------------------------------------------------------


def test_the_trail_is_the_ledger_with_reasons_and_gated_like_one(told):
    client, story, _events = told
    trip_id = story["trip_id"]

    # Alex's trip; the anonymous traveller cannot tell it exists.
    assert client.get("/api/trail", params={"trip": trip_id}).status_code == 404

    owner = client.post("/api/people", json={"name": "Reader"}).json()
    # A stranger with a real token: still 404.
    assert client.get("/api/trail", params={"trip": trip_id},
                      headers={"Authorization": f"Bearer {owner['token']}"}
                      ).status_code == 404


def test_a_host_may_not_read_the_trail_and_finance_may(client_world=None):
    client = TestClient(serve.app)
    owner = client.post("/api/people", json={"name": "Owner"}).json()
    auth = {"Authorization": f"Bearer {owner['token']}"}
    inbound = client.get("/api/search/flights", params={
        "origin": "SIN", "destination": "ZRH", "on": "2026-09-18"}).json()["offers"][0]
    trip = client.post("/api/select", headers=auth, json={
        "flights": [{"origin": "SIN", "destination": "ZRH", "on": "2026-09-18",
                     "offer_key": inbound["key"]}]}).json()

    given = {}
    for role in ("host", "finance"):
        given[role] = client.post("/api/share", headers=auth, json={
            "trip_id": trip["trip_id"], "name": role, "role": role}).json()

    host = {"Authorization": f"Bearer {given['host']['token']}"}
    fin = {"Authorization": f"Bearer {given['finance']['token']}"}
    assert client.get("/api/trail", params={"trip": trip["trip_id"]},
                      headers=host).status_code == 403
    read = client.get("/api/trail", params={"trip": trip["trip_id"]}, headers=fin)
    assert read.status_code == 200
    assert of(read.json()["events"], audit.EXTRACTED), "booking it left a record"

    # And the plain-text export reads as sentences, token in the URL allowed.
    text = client.get("/api/trail.txt", params={
        "trip": trip["trip_id"], "token": given["finance"]["token"]})
    assert text.status_code == 200
    assert "citation:" in text.text and "authorization:" in text.text
