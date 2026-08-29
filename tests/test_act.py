"""Acting on a plan, and being exact about which part was actually done.

Until this existed, every action in every plan was a label. The tests here are
mostly about the seam between what was performed and what was handed over,
because a system that blurs those two has started lying about the useful part.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

import serve                                        # noqa: E402

DAY = "2026-09-18"


@pytest.fixture(scope="module")
def client():
    return TestClient(serve.app)


def book(client, text="one way flight zurich to milan on 18 september "
                      "with a hotel, cheapest") -> dict:
    """A trip bought through the agent, the way a traveller buys one."""
    turn = client.post("/api/chat", json={"text": text}).json()
    while turn["asks"] and not turn["state"]["ready"]:
        field = turn["asks"][0]["field"]
        answer = {"hotel": "yes", "preference": "cheapest",
                  "ret": "one way"}.get(field, "yes")
        turn = client.post("/api/chat", json={"state": turn["state"],
                                              "answers": {field: answer}}).json()
    assert turn["flights"], turn.get("note")
    return client.post("/api/chat/choose", json={
        "state": turn["state"],
        "flight_id": turn["flights"][0]["id"],
        "hotel_id": turn["stays"][0]["id"] if turn["stays"] else ""}).json()


@pytest.fixture
def booked(client):
    return book(client)


def test_a_sentence_becomes_an_itinerary(client, booked):
    assert booked["trip_id"]
    kinds = {b["kind"] for b in booked["bookings"]}
    assert kinds == {"flight", "lodging"}
    assert booked["problems"] == []


def test_the_preference_is_stored_with_the_trip(client, booked):
    """So nobody has to ask a traveller what they care about at the moment
    their flight is cancelled."""
    assert booked["preference"] == "cheapest"
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    recovery = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()
    assert recovery["preference"] == "cheapest"


def test_several_itineraries_coexist(client):
    a, b = book(client), book(client)
    assert a["trip_id"] != b["trip_id"]
    ids = {i["trip_id"] for i in client.get("/api/itineraries").json()["itineraries"]}
    assert {a["trip_id"], b["trip_id"]} <= ids


def test_acting_sends_what_it_said_it_would_send(client, booked):
    """The AUTO lane's whole claim is 'Downstream handles it'. Before this it
    handled nothing -- the email to the property was a label."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    plans = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()["plans"]

    done = client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": plans[0]["id"]}).json()

    assert done["sent"], "acted and sent nothing"
    assert all(d["channels"] for d in done["sent"]), "claimed a send with no channel"
    assert done["log"], "sent without leaving a trace"


def test_acting_rewrites_the_itinerary(client, booked):
    """Adaptation, not notification. Come back tomorrow and the trip reflects
    the decision, or the system has quietly disagreed with itself."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    plans = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()["plans"]
    replacement = next(p for p in plans if p["id"] != "noop")

    done = client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": replacement["id"]}).json()

    titles = [b["title"] for b in done["bookings"]]
    assert leg["title"] not in titles, "the cancelled leg is still in the trip"
    assert replacement["name"] in titles, "the replacement never arrived"


def test_a_replacement_is_marked_as_not_yet_paid_for(client, booked):
    """A real booking in the plan and an intention in the record, and the two
    are not allowed to look alike -- including after a storage round trip."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    plans = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()["plans"]
    replacement = next(p for p in plans if p["id"] != "noop")
    client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": replacement["id"]})

    reloaded = next(i for i in client.get("/api/itineraries").json()["itineraries"]
                    if i["trip_id"] == booked["trip_id"])
    added = [b for b in reloaded["bookings"] if b["title"] == replacement["name"]]
    assert added and added[0]["pending"] is True
    assert all(b["pending"] is False for b in reloaded["bookings"]
               if b["title"] != replacement["name"])


def test_nothing_is_bought(client, booked):
    """Duffel's test mode cannot take money and we would not use it if it
    could. The purchase is handed over with a link and marked pending."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    plans = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()["plans"]
    replacement = next(p for p in plans if p["id"] != "noop")

    done = client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": replacement["id"]}).json()

    buys = [d for d in done["pending"] if d["verb"] == "buy"]
    assert buys and buys[0]["state"] == "pending"
    assert not any(d["verb"] == "buy" for d in done["sent"])


def test_acting_ends_the_disruption(client, booked):
    """The watch should stop counting down deadlines that have been dealt
    with."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    plans = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()["plans"]
    assert client.get("/api/alerts",
                      params={"trip": booked["trip_id"]}).json()["watching"]

    client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": plans[0]["id"]})

    assert not client.get("/api/alerts",
                          params={"trip": booked["trip_id"]}).json()["watching"]


def test_a_plan_that_is_not_on_this_disruption_is_refused(client, booked):
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    response = client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": "off_madeup"})
    assert response.status_code == 404


def test_an_expired_fare_is_refused_rather_than_substituted(client):
    turn = client.post("/api/chat", json={
        "text": "one way flight zurich to milan on 18 september, cheapest, no hotel"}).json()
    response = client.post("/api/chat/choose", json={
        "state": turn["state"], "flight_id": "off_gone"})
    assert response.status_code == 409
