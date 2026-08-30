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
#: The day the rail timetable was recorded for.
RAIL_DAY = "2026-10-12"
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


def test_a_train_can_be_bought_through_the_same_door_as_a_flight(client):
    """Rail was searchable by the engine and unbuyable by a person: no
    endpoint, no mode on a leg, and `select` re-resolved every leg by asking
    Duffel -- which does not sell trains, so the option came back withdrawn
    one request after it was offered."""
    found = client.get("/api/search/rail", params={
        "origin": "ZRH", "destination": "MXP", "on": RAIL_DAY}).json()
    assert found["offers"], "the recorded timetable has gone"
    train = found["offers"][0]
    assert train["mode"] == "rail" and train["estimated"] is True

    body = client.post("/api/select", json={
        "label": "by train",
        "flights": [{"origin": "ZRH", "destination": "MXP", "on": RAIL_DAY,
                     "mode": "rail", "offer_key": train["key"]}],
        "hotels": []}).json()
    assert body["problems"] == []
    leg = body["bookings"][0]
    assert leg["kind"] == "rail" and leg["price"] == train["price"]

    # And the whole engine applies from there, which is the only reason any of
    # this was worth wiring rather than special-casing.
    broken = client.post("/api/cancel", json={
        "trip_id": body["trip_id"], "booking_id": leg["id"]})
    assert broken.status_code == 200, broken.text


def test_a_route_with_no_train_says_so_rather_than_failing(client):
    """This reaches the Swiss timetable and nothing else, so most pairs of
    cities on earth have no train between them here. An empty list is the
    honest answer; a 503 would read as the software falling over."""
    body = client.get("/api/search/rail", params={
        "origin": "SIN", "destination": "BKK", "on": RAIL_DAY})
    assert body.status_code == 200
    assert body.json()["offers"] == []


def test_the_delay_button_reaches_the_engine(client):
    """The disruption the engine models best was the one a traveller could not
    produce. It existed only in the scripted demo, from a recorded status.

    Its own trip, not the module's: this files a disruption against whatever it
    is pointed at, and the shared one is read by every test after it.
    """
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    body = client.post("/api/delay", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"], "minutes": 240}).json()

    assert body["signal"]["cancelled"] is False
    assert body["signal"]["delay_minutes"] == 240
    assert body["signal"]["injected"] is True
    assert body["signal"]["now_arrives"]["iso"] > leg["ends"]["iso"]
    assert body["plans"], "a delay has to produce options like anything else"


# --------------------------------------------------------------------------
# what the agent may do on its own
# --------------------------------------------------------------------------


def _with_meeting(client, permissions: dict) -> dict:
    """A trip with a reason to exist: a client meeting in Milan the evening the
    onward hop lands. Cancelling that hop then makes a replacement worth more
    than it costs -- which is the only situation in which there is anything
    for the agent to take."""
    # The EARLIEST feasible onward hop, not the cheapest. Cancelling a 15:10
    # departure leaves no train in the day and the best plan is a EUR 555
    # flight -- correct, and useless for testing a EUR 300 cap. Cancelling a
    # morning hop leaves the EUR 72 train, which is the scenario the rules
    # were written for.
    inbound = client.get("/api/search/flights", params={
        "origin": "SIN", "destination": "ZRH", "on": DAY}).json()["offers"]
    first = min(inbound, key=lambda o: o["arrive"]["iso"])
    onward = client.get("/api/search/flights", params={
        "origin": "ZRH", "destination": "MXP", "on": DAY,
        "after": first["arrive"]["iso"]}).json()["offers"]
    stay = client.get("/api/search/hotels", params={
        "city": "Milan", "country": "IT",
        "check_in": DAY, "check_out": OUT}).json()["stays"][0]
    booked = None
    for candidate in sorted(onward, key=lambda o: o["depart"]["iso"]):
        response = client.post("/api/select", json={
            "label": "with a meeting", "permissions": permissions,
            "flights": [
                {"origin": "SIN", "destination": "ZRH", "on": DAY,
                 "offer_key": first["key"]},
                {"origin": "ZRH", "destination": "MXP", "on": DAY,
                 "offer_key": candidate["key"]}],
            "hotels": [{"city": "Milan", "country": "IT", "check_in": DAY,
                        "check_out": OUT, "hotel_id": stay["id"]}]}).json()
        if not response["problems"]:
            booked = response
            break
    assert booked, "no feasible morning hop in the recorded offers"
    turn = client.post("/api/chat", json={"text": "i have a meeting"}).json()
    replies = {"who": "the client", "day": DAY, "at": "10pm", "where": "MXP",
               "trip": booked["trip_id"], "confirm": "yes, save it"}
    for _ in range(8):
        pending = [a for a in turn.get("asks", []) if not a["optional"]]
        if not pending:
            break
        field = pending[0]["field"]
        turn = client.post("/api/chat", json={
            "state": turn["state"], "answers": {field: replies[field]}}).json()
    assert turn.get("saved", {}).get("ready"), turn.get("reply")
    return booked


def _break_onward(client, booked: dict) -> dict:
    onward = [b for b in booked["bookings"] if b["kind"] == "flight"][1]
    return client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": onward["id"]}).json()


def test_nothing_is_taken_without_rules(client):
    """The default has to be the behaviour before permissions existed: the
    plan is recommended and waits for a person."""
    outcome = _break_onward(client, _with_meeting(client, {}))
    assert outcome["plans"][0]["id"] != "noop", "the premise: something is worth taking"
    assert outcome["plans"][0]["auto"] is False
    assert "auto_taken" not in outcome
    assert "not pre-authorised" in outcome["why"]


def test_within_the_cap_the_agent_takes_the_plan_at_the_moment_of_disruption(client):
    """The brief's whole difference: a delay at 02:00 answered at 02:00 rather
    than at 07:30 when somebody presses a button. Applied, sent, rewritten --
    and what always needed a person still does, reported as waiting."""
    booked = _with_meeting(client, {"auto_limit": 300})
    outcome = _break_onward(client, booked)

    taken = outcome["auto_taken"]
    assert taken["by"] == "the agent"
    assert "within your EUR 300 limit" in taken["permission"]
    assert taken["pending"], "the purchase still needs a person, and must say so"
    assert all(d["verb"] != "buy" for d in taken["sent"]), "claimed to have bought"

    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    assert row["acted"]["by"] == "the agent", "taken and not recorded as the agent's doing"
    assert any(b["pending"] for b in row["bookings"]), "the replacement is on the itinerary"
    assert not row["disrupted"], "the disruption is dealt with"


def test_over_the_cap_the_agent_waits_and_says_by_how_much(client):
    outcome = _break_onward(client, _with_meeting(client, {"auto_limit": 50}))
    assert "auto_taken" not in outcome
    assert "exceeds your EUR 50 limit" in outcome["why"]
    buy = next(a for a in outcome["plans"][0]["actions"] if a["verb"] == "buy")
    assert buy["approval"] == "needs approval"


def test_never_removes_the_choice_and_says_so(client):
    """The train is the best plan for this trip. Ruled out, it is not silently
    missing -- the page is told what was withheld and why."""
    outcome = _break_onward(client, _with_meeting(client, {"auto_limit": 300, "never": ["rail"]}))
    assert all(p["mode"] != "rail" for p in outcome["plans"])
    assert outcome["excluded"] and outcome["excluded"][0]["why"] == "you said never rail"


def test_the_rationale_names_the_goal_and_its_price(client):
    """"Recommended because it keeps the meeting; costs EUR 72 more than doing
    nothing, which would miss it." Every clause has to be true of this plan and
    taken from the engine."""
    outcome = _break_onward(client, _with_meeting(client, {}))
    why = outcome["why"]
    assert why.startswith("Recommended because it keeps Meeting with the client")
    assert "which would miss it" in why
    best = outcome["plans"][0]
    assert best["saves"] and not best["misses"]
    noop = next(p for p in outcome["plans"] if p["id"] == "noop")
    assert noop["misses"], "doing nothing loses the meeting, and the payload says so"


def test_rules_can_be_changed_after_booking_and_are_listed(client):
    booked = book(client)
    changed = client.post("/api/permissions", json={
        "trip_id": booked["trip_id"], "auto_limit": 120, "never": ["Rail"]}).json()
    assert changed["permissions"]["auto_limit"] == 120
    assert changed["permissions"]["never"] == ["rail"]
    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    assert row["permissions"] == changed["permissions"]
    assert client.post("/api/permissions", json={"trip_id": "nope"}).status_code == 404


def test_pricing_a_cancellation_sends_nothing(client):
    """The only action in this product that destroys value on purpose and
    cannot be undone by pressing it again. The first call asks what it would
    cost; nothing leaves the building and the trip is still live."""
    booked = book(client)
    quote = client.post("/api/abandon", json={"trip_id": booked["trip_id"]}).json()

    assert quote["confirmed"] is False
    assert "sent" not in quote and "pending" not in quote
    assert quote["plan"]["actions"], "priced nothing"

    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    assert row["abandoned"] is None, "priced it and cancelled it anyway"


def test_the_ledger_of_calling_it_off_balances(client):
    """What comes back plus what is gone is what was paid. Nothing else is an
    acceptable answer to "what does this cost me"."""
    booked = book(client)
    quote = client.post("/api/abandon", json={"trip_id": booked["trip_id"]}).json()
    assert round(quote["refund"] + quote["lost"], 2) == quote["paid"]
    assert quote["paid"] == booked["total"]


def test_every_booking_gets_an_action_and_a_lane(client):
    """One errand per booking, and each in the lane that can actually perform
    it. A row with no action is a booking the traveller will discover they
    still hold, at the airport."""
    booked = book(client)
    quote = client.post("/api/abandon", json={"trip_id": booked["trip_id"]}).json()

    acted = {a["booking_id"] for a in quote["plan"]["actions"]}
    assert acted == {b["id"] for b in booked["bookings"]}
    assert all(a["lane"] in ("auto", "tap", "call") for a in quote["plan"]["actions"])


def test_calling_it_off_performs_the_auto_lane_and_hands_over_the_rest(client):
    """The lanes stop being labels here exactly as they do for a recovery: what
    Downstream can send is sent, and what needs a person is handed over marked
    pending rather than reported as done."""
    booked = book(client)
    done = client.post("/api/abandon", json={
        "trip_id": booked["trip_id"], "confirm": True,
        "reason": "the meeting moved"}).json()

    assert done["confirmed"] is True
    assert len(done["sent"]) + len(done["pending"]) == len(done["plan"]["actions"])
    assert all(d["state"] == "pending" for d in done["pending"])

    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    assert row["abandoned"]["reason"] == "the meeting moved"
    assert row["abandoned"]["refund"] == done["refund"]
    assert row["abandoned"]["actions"], "cancelled it and kept no record of what was owed"


def test_every_row_reads_as_cancelled_once_the_trip_is(client):
    """The hotel gave it away. A cancelled itinerary kept every row reading
    like a live booking, and the room is the row with no buttons on it to look
    disabled -- so somebody who had just called the trip off was still looking
    at a bed they had been told they had."""
    booked = book(client)
    client.post("/api/abandon", json={"trip_id": booked["trip_id"], "confirm": True})

    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    assert all(b["cancelled"] for b in row["bookings"]), "a row survived the cancellation"

    stay = next(b for b in row["bookings"] if b["kind"] == "lodging")
    assert stay["cancelled"]["label"], "the room is cancelled and says nothing about it"
    assert stay["cancelled"]["lane"] in ("auto", "tap", "call")


def test_the_errand_on_a_row_is_the_one_for_that_booking(client):
    """Every row carries its own, not the trip's summary. "Cancel the flight"
    under the hotel is worse than nothing on it at all."""
    booked = book(client)
    done = client.post("/api/abandon", json={
        "trip_id": booked["trip_id"], "confirm": True}).json()

    for b in done["bookings"]:
        assert b["cancelled"]["booking_id"] == b["id"]


def test_every_door_a_tester_is_given_opens(client):
    """Four URLs go to people who did not build this. A 404 on any of them is
    the first thing they will see and the last thing they will report."""
    for path in ("/", "/demo", "/book", "/test"):
        assert client.get(path).status_code == 200, path


def test_the_test_guide_reads_the_instance_rather_than_asserting_it(client):
    """Which routes exist at all depends on whether this instance is talking to
    providers or replaying recordings, and that changes per deploy. A guide that
    states it in prose goes stale silently and sends everybody hunting the wrong
    bug, so the page reads /health and /health has to keep answering."""
    page = client.get("/test").text
    assert "/health" in page, "the guide stopped reading the instance"

    health = client.get("/health").json()
    assert health["ports_mode"] in ("replay", "live", "record")
    for key in ("model_status", "storage", "degraded", "shifted"):
        assert key in health, f"the guide renders {key} and health stopped sending it"
    assert "ready" in health["model_status"]


def test_the_quote_carries_what_a_page_needs_to_argue_it(client):
    """The chat panel prices this into the thread rather than into a sidebar,
    which means the answer has to name the trip, group the work by who does it,
    and say what moment it was true at."""
    booked = book(client)
    quote = client.post("/api/abandon", json={"trip_id": booked["trip_id"]}).json()

    assert quote["label"] == booked["label"]
    assert set(quote["plan"]["lanes"]) == {"auto", "tap", "call"}
    assert quote["as_of"] and quote["as_of"]["iso"]
    assert sum(len(v) for v in quote["plan"]["lanes"].values()) \
        == len(quote["plan"]["actions"])


def test_nothing_to_recover_is_zero_and_not_minus_zero(client):
    """Negating a zero net gives -0.0, and a page that renders it faithfully
    tells the traveller they are getting "-EUR 0" back."""
    booked = book(client)
    quote = client.post("/api/abandon", json={"trip_id": booked["trip_id"]}).json()
    import math
    assert not math.copysign(1, quote["refund"]) < 0 or quote["refund"] != 0


def test_a_cancelled_trip_cannot_be_disrupted(client):
    """Delaying a flight on a trip nobody is taking would start the watch
    counting down deadlines the traveller has already been told are gone."""
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    client.post("/api/abandon", json={"trip_id": booked["trip_id"], "confirm": True})

    for path, body in (("/api/cancel", {}), ("/api/delay", {"minutes": 90})):
        response = client.post(path, json={
            "trip_id": booked["trip_id"], "booking_id": leg["id"], **body})
        assert response.status_code == 409, path


def test_a_live_trip_has_no_errands_on_its_rows(client):
    """The other half. `cancelled` is None until somebody cancels, or every
    booking ever made reads as an obligation."""
    booked = book(client)
    assert all(b["cancelled"] is None for b in booked["bookings"])
    quote = client.post("/api/abandon", json={"trip_id": booked["trip_id"]}).json()
    assert all(b["cancelled"] is None for b in quote["bookings"]), (
        "priced it and marked the rows anyway")


def test_a_cancelled_trip_cannot_be_cancelled_again(client):
    """Pressing it twice must not send the property a second email or promise a
    second refund."""
    booked = book(client)
    client.post("/api/abandon", json={"trip_id": booked["trip_id"], "confirm": True})
    again = client.post("/api/abandon", json={"trip_id": booked["trip_id"], "confirm": True})
    assert again.status_code == 409


def test_calling_it_off_stops_the_watch(client):
    """A trip nobody is taking has no deadlines worth counting down to."""
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]})
    assert client.get("/api/alerts",
                      params={"trip": booked["trip_id"]}).json()["watching"]

    client.post("/api/abandon", json={"trip_id": booked["trip_id"], "confirm": True})
    assert not client.get("/api/alerts",
                          params={"trip": booked["trip_id"]}).json()["watching"]


def test_a_delayed_leg_says_so_on_the_itinerary(client):
    """The page showed a flight at its original times with nothing to say it
    was running three hours late, which is the one fact it was opened for.
    /api/itineraries knew the trip was disrupted and not what had happened."""
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    assert leg["disruption"] is None, "nothing has happened to it yet"

    client.post("/api/delay", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"], "minutes": 195})
    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    hit = next(b["disruption"] for b in row["bookings"] if b["id"] == leg["id"])

    assert hit["cancelled"] is False
    assert hit["delay_minutes"] == 195
    assert hit["now_arrives"]["iso"] > hit["was"]["iso"]
    # And only the leg it happened to.
    assert all(b["disruption"] is None for b in row["bookings"] if b["id"] != leg["id"])


def test_a_delay_survives_the_page_being_refreshed(client):
    """It was drawn from the last disruption response, which is a fact about
    one browser tab rather than about the trip -- so the label and the new time
    vanished on a refresh while the disruption sat in storage the whole time.

    A refresh is exactly this: nothing in hand but a trip id, and everything
    that has to be shown fetched again."""
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    answered = client.post("/api/delay", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "minutes": 195}).json()

    # One source: what the disruption endpoint hands back and what the page
    # fetches on reload have to be the same rows, or they will drift.
    reloaded = next(t for t in client.get("/api/itineraries").json()["itineraries"]
                    if t["trip_id"] == booked["trip_id"])
    assert answered["bookings"] == reloaded["bookings"]

    hit = next(b["disruption"] for b in reloaded["bookings"] if b["id"] == leg["id"])
    assert hit["delay_minutes"] == 195
    assert hit["now_arrives"] and hit["was"]


def test_a_restored_trip_carries_what_it_takes_to_draw_it(client):
    """Half a restored trip -- rows with no reason they cannot be taken -- is
    worse than none, and a page that cannot tell whether storage is durable
    warns about losing a trip it will not lose."""
    booked = book(client)
    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    assert row["problems"] == booked["problems"]
    assert isinstance(row["durable"], bool)
    assert row["total"] == booked["total"]


def test_the_scheduled_times_are_not_overwritten_by_the_delay(client):
    """`starts` and `ends` are what was BOOKED and they do not move. A delay is
    what the world is doing to the schedule, not a correction of it, and
    replacing the arrival with a predicted one erases the comparison the
    traveller is trying to make."""
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    client.post("/api/delay", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"], "minutes": 240})

    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    after = next(b for b in row["bookings"] if b["id"] == leg["id"])
    assert after["starts"] == leg["starts"] and after["ends"] == leg["ends"]
    assert after["disruption"]["was"] == leg["ends"]


def test_a_cancelled_leg_is_not_reported_as_running_late(client):
    """There is no arrival to be late for, so the row must not offer one."""
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    client.post("/api/cancel", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"]})

    row = next(t for t in client.get("/api/itineraries").json()["itineraries"]
               if t["trip_id"] == booked["trip_id"])
    hit = next(b["disruption"] for b in row["bookings"] if b["id"] == leg["id"])
    assert hit["cancelled"] is True
    assert hit["delay_minutes"] == 0 and hit["now_arrives"] is None


def test_a_delay_of_nothing_is_refused(client, booked):
    """Zero minutes is not a disruption, and storing one would leave the watch
    counting down to a flight that is running exactly on time.

    Safe on the shared trip precisely because it is refused: nothing is filed.
    """
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    for minutes in (0, -30):
        response = client.post("/api/delay", json={
            "trip_id": booked["trip_id"], "booking_id": leg["id"],
            "minutes": minutes})
        assert response.status_code == 422


def test_acting_on_a_delay_does_not_rebuild_it_as_a_cancellation(client):
    """`/api/act` re-derived the recovery to search again, and derived it by
    cancelling -- harmless while cancelling was the only thing anybody could
    inject, and a lie the moment a traveller says "late" instead.

    Its own trip: acting REWRITES the itinerary, which is the point of it.
    """
    booked = book(client)
    leg = [b for b in booked["bookings"] if b["kind"] == "flight"][0]
    late = client.post("/api/delay", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"], "minutes": 600}).json()
    assert late["plans"]

    plan = late["plans"][0]
    done = client.post("/api/act", json={
        "trip_id": booked["trip_id"], "booking_id": leg["id"],
        "plan_id": plan.get("key") or plan["id"]})
    assert done.status_code == 200, done.text

    # Rebuilding it as a cancellation showed up here: `act.apply` drops the
    # disrupted leg, so the traveller was handed an itinerary missing the
    # flight they are about to board. It is late, not gone.
    after = {b["id"] for b in done.json()["bookings"]}
    assert leg["id"] in after, "deleted a flight the traveller is still taking"


def test_every_row_can_say_when_it_starts_and_when_it_ends(client, booked):
    """A row that shows only a start makes the reader supply the other half.
    "Hotel, 18 Sep 14:00" is a stay of unknown length, and the length is the
    thing a disrupted traveller is trying to work out."""
    for row in booked["bookings"]:
        assert row["starts"] and row["ends"], row["title"]
        assert {"iso", "t", "d"} <= set(row["ends"])


def test_the_impact_cards_carry_the_times_too(client):
    """The knock-on map is an itinerary as much as the detail panel is, and it
    was showing a cutoff with no start -- so placing a booking in the trip
    meant holding the trip in your head."""
    state = client.get("/api/state", params={"base": "written"}).json()
    nodes = state["impact"]["nodes"]
    assert nodes
    for node in nodes:
        assert node["starts"] and node["ends"], node["title"]


def test_the_scripted_trip_leaves_no_row_half_told():
    """Eight of its twelve bookings had no end at all. The engine never read
    one, so nothing failed -- it just could not be displayed, and the demo is
    the one itinerary anybody reads line by line."""
    from demo_trip import build_trip

    for booking in build_trip(None).in_order():
        assert booking.end is not None, booking.title
        assert booking.end >= booking.start, booking.title


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

    # Qualified by trip: an alert key is unique within an itinerary and not
    # across them, because booking ids are derived from titles and two trips
    # can hold the same hotel on the same night. Those are two things at stake,
    # not one alert sent twice.
    sent = []
    monkeypatch.setattr(app_module.notify, "deliver",
                        lambda alert, trip_id="", **kw:
                        sent.append((trip_id, alert.key)) or ["log"])
    first = app_module.sweep(now=tick)
    second = app_module.sweep(now=tick)
    assert first > 0
    assert second == 0
    assert len(sent) == len(set(sent))
    assert any(t == booked["trip_id"] for t, _ in sent)


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
