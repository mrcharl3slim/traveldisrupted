"""The endpoints, end to end, over the same recordings the engine tests use.

These exist because the seams between HTTP and the engine are where the
scenario quietly stops being real: an id that does not survive storage, a date
that loses its timezone, a payload shape that differs between the live path and
the injected one. None of those show up in an engine test.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

import serve                                        # noqa: E402

DAY = "2026-09-18"
OUT = "2026-09-20"


@pytest.fixture(scope="module")
def client():
    return TestClient(serve.app)


@pytest.fixture(scope="module")
def booked(client):
    """A trip bought through the API, exactly as the page would buy it."""
    inbound = client.get("/api/search/flights", params={
        "origin": "SIN", "destination": "ZRH", "on": DAY}).json()["offers"]
    first = min(inbound, key=lambda o: o["arrive"]["iso"])
    onward = client.get("/api/search/flights", params={
        "origin": "ZRH", "destination": "MXP", "on": DAY}).json()["offers"]
    stay = client.get("/api/search/hotels", params={
        "city": "Milan", "country": "IT",
        "check_in": DAY, "check_out": OUT}).json()["stays"][0]

    for candidate in sorted(onward, key=lambda o: o["price"]):
        response = client.post("/api/select", json={
            "label": "test",
            "flights": [
                {"origin": "SIN", "destination": "ZRH", "on": DAY,
                 "offer_id": first["id"]},
                {"origin": "ZRH", "destination": "MXP", "on": DAY,
                 "offer_id": candidate["id"]}],
            "hotels": [{"city": "Milan", "country": "IT", "check_in": DAY,
                        "check_out": OUT, "hotel_id": stay["id"]}]})
        assert response.status_code == 200, response.text
        if not response.json()["problems"]:
            return response.json()
    pytest.fail("no feasible selection in the recorded offers")


def test_a_search_quotes_fares_and_says_where_they_came_from(client):
    body = client.get("/api/search/flights", params={
        "origin": "ZRH", "destination": "MXP", "on": DAY}).json()
    assert body["offers"]
    for offer in body["offers"]:
        assert offer["price"] > 0
        assert offer["price_source"] in ("quoted", "converted")
        assert offer["estimated"] is (offer["price_source"] != "quoted")


def test_a_stay_carries_the_deadline_that_matters(client):
    stays = client.get("/api/search/hotels", params={
        "city": "Milan", "country": "IT",
        "check_in": DAY, "check_out": OUT}).json()
    assert stays["code"] == "MXP"
    for stay in stays["stays"]:
        # 14:00 opens the desk; 22:00 is when the room stops being held. Only
        # the second one is a deadline, and it is not in any rate response.
        assert stay["held_until"]["t"] == "22:00"
        assert stay["check_in"]["t"] == "14:00"


def test_the_ids_handed_out_are_the_ids_that_can_be_cancelled(client, booked):
    """Booking ids are derived from the title when a trip is read back, so the
    ids the assembler minted named bookings no later request could find --
    cancelling a leg answered "no booking with that id" for a leg the client
    had just been shown."""
    for booking in booked["bookings"]:
        response = client.post("/api/cancel", json={
            "trip_id": booked["trip_id"], "booking_id": booking["id"]})
        assert response.status_code == 200, booking["id"]


def test_an_unknown_booking_is_a_404_not_a_crash(client, booked):
    response = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": "nope"})
    assert response.status_code == 404


def test_a_stale_offer_is_refused_rather_than_substituted(client):
    """Fares move. Silently booking the nearest thing is how a traveller ends up
    holding a ticket they did not choose."""
    response = client.post("/api/select", json={
        "flights": [{"origin": "SIN", "destination": "ZRH", "on": DAY,
                     "offer_id": "off_notarealoffer"}]})
    assert response.status_code == 409
    assert "search again" in response.json()["detail"]


def test_cancelling_answers_in_the_same_shape_as_a_live_delay(client, booked):
    """One payload for both paths. Two shapes would let them drift, and the one
    that drifts is always the one nobody demos."""
    live = client.get("/api/state", params={"base": "written"}).json()
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    injected = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()

    assert live["disrupted"] and injected["disrupted"]
    assert set(live) <= set(injected)
    assert live["signal"]["injected"] is False
    assert injected["signal"]["injected"] is True
    assert injected["signal"]["cancelled"] is True


def test_the_gap_and_the_search_agree(client, booked):
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    body = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()

    gap = body["gap"]
    assert (gap["origin"], gap["destination"]) == ("ZRH", "MXP")
    assert body["searched"], "the gap was derived and then nothing was searched"
    assert all(o["origin"] == gap["origin"] for o in body["searched"])


def test_the_cancelled_flight_never_becomes_a_plan(client, booked):
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    body = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()

    itself = [o["id"] for o in body["searched"]
              if o["depart"]["iso"] == leg["starts"]["iso"]
              and o["label"].split(" - ")[0] == leg["title"].split(" - ")[0]]
    assert itself, "the recording no longer contains the cancelled flight"
    assert not (set(itself) & {p["id"] for p in body["plans"]})


def test_doing_nothing_is_always_on_the_table(client, booked):
    """It has to be beatable, not absent. A recovery product that never offers
    inaction is a shop."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    body = client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]}).json()
    assert any(p["id"] == "noop" for p in body["plans"])
