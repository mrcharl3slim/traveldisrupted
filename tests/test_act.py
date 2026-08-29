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
        "flight_key": turn["flights"][0]["key"],
        "flight_price": turn["flights"][0]["price"],
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
        "plan_key": plans[0]["key"]}).json()

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
        "plan_key": replacement["key"]}).json()

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
        "plan_key": replacement["key"]})

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
        "plan_key": replacement["key"]}).json()

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
        "plan_key": plans[0]["key"]})

    assert not client.get("/api/alerts",
                          params={"trip": booked["trip_id"]}).json()["watching"]


def test_a_plan_that_is_not_on_this_disruption_is_refused(client, booked):
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    response = client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_key": "ZZ 9999|ZRH|MXP|2026-09-18T09:00:00+02:00"})
    assert response.status_code == 409


def test_a_withdrawn_flight_is_refused_rather_than_substituted(client):
    """Silently booking the nearest thing is how somebody ends up holding a
    ticket they did not choose."""
    turn = client.post("/api/chat", json={
        "text": "one way flight zurich to milan on 18 september, cheapest, no hotel"}).json()
    response = client.post("/api/chat/choose", json={
        "state": turn["state"],
        "flight_key": "ZZ 9999|ZRH|MXP|2026-09-18T09:00:00+02:00",
        "flight_price": 100.0})
    assert response.status_code == 409
    assert "no longer being sold" in response.json()["detail"]


def test_a_selection_survives_the_search_that_found_it(client):
    """The bug that broke every booking the moment the ports went live. Duffel
    mints offer ids per offer request, so the id shown to the traveller does
    not exist in the search `choose` runs a moment later. Matching on what the
    flight IS survives that; matching on the id could not."""
    turn = client.post("/api/chat", json={
        "text": "one way flight zurich to milan on 18 september, cheapest, no hotel"}).json()
    shown = turn["flights"][0]

    saved = client.post("/api/chat/choose", json={
        "state": turn["state"], "flight_key": shown["key"],
        "flight_price": shown["price"],
        # An id from a search that no longer exists, exactly as live Duffel
        # would leave it. The key has to carry the selection on its own.
        "flight_id": "off_0000fromanoldofferrequest"})
    assert saved.status_code == 200, saved.text
    assert shown["label"] in [b["title"] for b in saved.json()["bookings"]]


def test_the_same_flight_at_two_fares_picks_the_one_shown(client):
    """A key names an aircraft, not a price: the same flight is sold under
    several fare brands, and JU 0333 came back at both EUR 170 and EUR 255 in
    one search. The traveller meant the number they were looking at."""
    turn = client.post("/api/chat", json={
        "text": "one way flight zurich to milan on 18 september, cheapest, no hotel"}).json()
    by_key: dict[str, list] = {}
    for offer in turn["flights"]:
        by_key.setdefault(offer["key"], []).append(offer)
    shared = [rows for rows in by_key.values() if len(rows) > 1]
    if not shared:
        pytest.skip("no duplicate fare brands in the recorded search")

    dearer = max(shared[0], key=lambda o: o["price"])
    saved = client.post("/api/chat/choose", json={
        "state": turn["state"], "flight_key": dearer["key"],
        "flight_price": dearer["price"]}).json()
    booked_price = [b["price"] for b in saved["bookings"]
                    if b["kind"] == "flight"][0]
    assert booked_price == dearer["price"]


def test_a_fare_that_moved_is_reported_not_swallowed(client):
    """Between the click and the booking, a price can change. That is ordinary
    and it is the traveller's business."""
    turn = client.post("/api/chat", json={
        "text": "one way flight zurich to milan on 18 september, cheapest, no hotel"}).json()
    shown = turn["flights"][0]

    saved = client.post("/api/chat/choose", json={
        "state": turn["state"], "flight_key": shown["key"],
        # What they saw a moment ago, before it moved.
        "flight_price": shown["price"] - 25}).json()
    assert saved["repriced"], "booked at a different price and said nothing"
    assert saved["repriced"][0]["now"] == shown["price"]
    assert saved["repriced"][0]["moved"] == 25


def test_an_unreadable_answer_is_acknowledged_not_repeated(client):
    """Over HTTP, where the traveller actually meets it."""
    turn = client.post("/api/chat", json={
        "text": "flight from singapore to london on 1 september"}).json()
    assert turn["asks"][0]["field"] == "ret"

    stuck = client.post("/api/chat", json={
        "state": turn["state"], "answers": {"ret": "sometime probably"}}).json()
    assert stuck["unread"] == ["ret"]
    assert stuck["reply"].lower().startswith("sorry")

    moved = client.post("/api/chat", json={
        "state": turn["state"], "answers": {"ret": "coming back"}}).json()
    assert moved["unread"] == []
    assert moved["asks"][0]["field"] == "ret_date"
    assert moved["reply"] != turn["reply"], "asked the same question again"


def test_a_typed_reply_answers_the_open_question(client):
    """The bug a traveller actually hit. Tapping "coming back" sent an answer
    and worked; TYPING the same two words sent a message, and a message was
    parsed as a fresh booking request -- which has no city and no date, so
    nothing merged and the same question came back. They had answered
    correctly, twice, and were asked a third time."""
    turn = client.post("/api/chat", json={
        "text": "flight from singapore to london on 1 september"}).json()
    assert turn["asks"][0]["field"] == "ret"

    typed = client.post("/api/chat", json={
        "state": turn["state"], "text": "two way"}).json()
    assert typed["state"]["one_way"] is False
    assert typed["asks"][0]["field"] == "ret_date"
    assert typed["reply"] != turn["reply"]


def test_typing_walks_the_whole_conversation(client):
    """Chips are a shortcut, not the only path. Every question has to be
    answerable by typing, because that is what a person does first."""
    turn = client.post("/api/chat", json={
        "text": "flight from zurich to milan on 18 september"}).json()
    for said in ("one way", "yes", "cheapest"):
        turn = client.post("/api/chat", json={
            "state": turn["state"], "text": said}).json()
    assert turn["state"]["ready"]
    assert turn["flights"], "walked the whole conversation and searched nothing"
    assert turn["state"]["preference"] == "cheapest"
    assert turn["state"]["hotel"] is True


def test_an_unhandled_error_is_still_readable_json(client, monkeypatch):
    """Starlette's default for a crash is plain text, which a JSON client
    reports as "unreadable response" — three words that describe every proxy
    error page in existence and identify none of them."""
    import serve as app_module

    def boom(*_a, **_k):
        raise RuntimeError("something specific went wrong")

    monkeypatch.setattr(app_module.converse, "turn", boom)
    # raise_server_exceptions=False so the test sees what a browser sees,
    # rather than the exception the test client helpfully re-raises.
    browser = TestClient(app_module.app, raise_server_exceptions=False)
    response = browser.post("/api/chat", json={"text": "flight to milan"})
    assert response.status_code == 500
    body = response.json()                       # must parse, that is the point
    assert "something specific went wrong" in body["detail"]
    assert "RuntimeError" in body["detail"]
