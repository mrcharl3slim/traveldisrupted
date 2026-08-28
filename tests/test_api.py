"""The endpoints, end to end, over the same recordings the engine tests use.

These exist because the seams between HTTP and the engine are where the
scenario quietly stops being real: an id that does not survive storage, a date
that loses its timezone, a payload shape that differs between the live path and
the injected one. None of those show up in an engine test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

import serve                                        # noqa: E402

DAY = "2026-09-18"
OUT = "2026-09-20"


@pytest.fixture(scope="module")
def client():
    return TestClient(serve.app)


def book(client) -> dict:
    """Buy a trip through the API, exactly as the page would buy it."""
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


@pytest.fixture(scope="module")
def booked(client):
    return book(client)


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


# --------------------------------------------------------------------------
# the watch
# --------------------------------------------------------------------------


def test_a_trip_with_no_disruption_has_no_clock(client):
    """Its own trip on purpose. The shared one has been cancelled by an earlier
    test, and a watch test that depends on file order is not a watch test."""
    fresh = book(client)
    body = client.get("/api/alerts", params={"trip": fresh["trip_id"]}).json()
    assert body["watching"] is False
    assert body["alerts"] == []


def test_cancelling_starts_the_clock(client, booked):
    """The disruption is written down, not held in the request that made it.
    Without that the watch has nothing to watch a minute after the page that
    started it was closed."""
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]})

    body = client.get("/api/alerts", params={"trip": booked["trip_id"]}).json()
    assert body["watching"] is True
    assert body["alerts"]
    assert all(a["worth"] > 0 for a in body["alerts"])


def test_the_page_is_told_which_channels_would_actually_reach_you(client, booked):
    body = client.get("/api/alerts", params={"trip": booked["trip_id"]}).json()
    assert "log" in body["channels"]
    health = client.get("/health").json()
    assert health["watch"]["durable"] is False
    assert "sleeps" in health["watch"]["caveat"]


def test_a_sweep_fires_each_alert_once(client, booked, monkeypatch):
    """Rebuilt schedule, persisted watermark. Two sweeps in a row must not say
    the same thing twice -- that is the whole difference between a notifier
    somebody keeps and one they mute."""
    import serve as app_module

    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]})

    schedule = client.get("/api/alerts",
                          params={"trip": booked["trip_id"]}).json()["alerts"]
    # A moment just after the first thing this trip has to say. Injected rather
    # than waited for: the trip is three weeks out, and a notifier whose only
    # test is patience does not get tested.
    tick = datetime.fromisoformat(schedule[0]["at"]["iso"]) + timedelta(minutes=1)

    sent = []
    monkeypatch.setattr(app_module.notify, "deliver",
                        lambda alert, trip_id="", **kw: sent.append(alert.key) or ["log"])
    first = app_module.sweep(now=tick)
    second = app_module.sweep(now=tick)
    assert first > 0
    assert second == 0
    assert len(sent) == len(set(sent))


def test_an_unreadable_disruption_does_not_end_the_watch(client, booked):
    """One bad payload cannot be allowed to stop the loop for every other trip
    in the process."""
    import serve as app_module
    import store as store_module

    saved = store_module.store().get(booked["trip_id"])
    payload = dict(saved.payload)
    payload["disruption"] = {"booking_id": "gone", "new_end": "not a date"}
    store_module.store().update(booked["trip_id"], payload)

    assert app_module.sweep(now=datetime.now(timezone.utc)) == 0
    body = client.get("/api/alerts", params={"trip": booked["trip_id"]}).json()
    assert body["watching"] is False
