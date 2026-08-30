"""Who may see and do what on a trip -- by what the job needs.

Every test here is one of three sentences. A person not let in cannot tell
the trip exists. A person let in as a role can do what that role needs and
nothing more. A person who may not see money sees the same trip with every
number blanked, and cannot get a number out by asking a different endpoint.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

import roles                                        # noqa: E402
import serve                                        # noqa: E402

DAY = "2026-09-18"


@pytest.fixture(scope="module")
def client():
    return TestClient(serve.app)


def as_(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def person(client, name: str) -> dict:
    return client.post("/api/people", json={"name": name}).json()


def book(client, token: str) -> dict:
    """A trip bought by whoever holds the token."""
    inbound = client.get("/api/search/flights", params={
        "origin": "SIN", "destination": "ZRH", "on": DAY}).json()["offers"]
    first = min(inbound, key=lambda o: o["arrive"]["iso"])
    onward = client.get("/api/search/flights", params={
        "origin": "ZRH", "destination": "MXP", "on": DAY,
        "after": first["arrive"]["iso"]}).json()["offers"]
    stay = client.get("/api/search/hotels", params={
        "city": "Milan", "country": "IT", "check_in": DAY,
        "check_out": "2026-09-20"}).json()["stays"][0]
    for candidate in sorted(onward, key=lambda o: o["price"]):
        body = client.post("/api/select", headers=as_(token), json={
            "label": "shared", "flights": [
                {"origin": "SIN", "destination": "ZRH", "on": DAY, "offer_key": first["key"]},
                {"origin": "ZRH", "destination": "MXP", "on": DAY, "offer_key": candidate["key"]}],
            "hotels": [{"city": "Milan", "country": "IT", "check_in": DAY,
                        "check_out": "2026-09-20", "hotel_id": stay["id"]}]}).json()
        if not body["problems"]:
            return body
    pytest.fail("no feasible selection in the recorded offers")


def share(client, owner: str, trip_id: str, name: str, role: str) -> dict:
    response = client.post("/api/share", headers=as_(owner), json={
        "trip_id": trip_id, "name": name, "role": role})
    assert response.status_code == 200, response.text
    return response.json()


def rows(client, token: str) -> list:
    return client.get("/api/itineraries", headers=as_(token)).json()["itineraries"]


@pytest.fixture(scope="module")
def world(client):
    """One owner, one trip, one person per role, one stranger."""
    owner = person(client, "Charles")
    trip = book(client, owner["token"])
    members = {r: share(client, owner["token"], trip["trip_id"], f"the {r}", r)
               for r in ("assistant", "organiser", "finance", "host", "contact")}
    stranger = person(client, "Nobody")
    return {"owner": owner, "trip": trip, "members": members, "stranger": stranger}


# --------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------


def test_the_roles_are_named_by_what_the_job_needs():
    assert roles.can("owner", roles.SHARE) and not roles.can("assistant", roles.SHARE)
    assert roles.can("assistant", roles.ABANDON) and not roles.can("organiser", roles.ABANDON)
    assert roles.can("finance", roles.RULES) and not roles.can("finance", roles.ACT)
    assert roles.can("host", roles.VIEW) and not roles.can("host", roles.MONEY)
    assert roles.ROLES["host"] == roles.ROLES["contact"], "same needs, kept as two names"
    assert not roles.can("nobody", roles.VIEW)


def test_redaction_walks_the_whole_payload():
    deep = {"price": 1, "plans": [{"net_cash": 2, "lanes": {"tap": [{"cash_in": 3}]}}],
            "title": "kept"}
    seen = roles.redact(deep, "host")
    assert seen["title"] == "kept"
    assert seen["price"] is None
    assert seen["plans"][0]["net_cash"] is None
    assert seen["plans"][0]["lanes"]["tap"][0]["cash_in"] is None
    assert roles.redact(deep, "finance") == deep


# --------------------------------------------------------------------------
# who can see
# --------------------------------------------------------------------------


def test_a_stranger_cannot_tell_the_trip_exists(client, world):
    trip_id = world["trip"]["trip_id"]
    token = world["stranger"]["token"]
    assert trip_id not in {t["trip_id"] for t in rows(client, token)}
    for path, body in (("/api/cancel", {"booking_id": "x"}),
                       ("/api/abandon", {}), ("/api/permissions", {"auto_limit": 1})):
        assert client.post(path, headers=as_(token),
                           json={"trip_id": trip_id, **body}).status_code == 404, path
    assert client.get("/api/alerts", headers=as_(token),
                      params={"trip": trip_id}).status_code == 404


def test_a_token_nobody_issued_is_refused_not_treated_as_anonymous(client):
    assert client.get("/api/itineraries", headers=as_("made-up")).status_code == 401


def test_no_token_is_the_anonymous_traveller_as_before(client, world):
    """Everything that worked before roles existed still works without one --
    and the anonymous traveller does not see somebody else's trip."""
    body = client.get("/api/itineraries").json()
    assert body["me"]["anonymous"] is True
    assert world["trip"]["trip_id"] not in {t["trip_id"] for t in body["itineraries"]}


def test_everyone_let_in_sees_the_same_trip_and_knows_their_role(client, world):
    trip_id = world["trip"]["trip_id"]
    for role, m in world["members"].items():
        row = next(t for t in rows(client, m["token"]) if t["trip_id"] == trip_id)
        assert row["role"] == role
        assert set(row["can"]) == set(roles.ROLES[role]) | (
            {roles.RULES} if roles.can(role, roles.RULES) else set())
    mine = next(t for t in rows(client, world["owner"]["token"]) if t["trip_id"] == trip_id)
    assert mine["role"] == "owner"
    assert {m["role"] for m in mine["members"]} == set(world["members"])


# --------------------------------------------------------------------------
# who can see money
# --------------------------------------------------------------------------


def test_a_host_sees_where_and_when_and_never_what_it_cost(client, world):
    trip_id = world["trip"]["trip_id"]
    host = world["members"]["host"]["token"]
    row = next(t for t in rows(client, host) if t["trip_id"] == trip_id)
    assert row["members"] is None, "a host does not get the guest list"
    assert row["total"] is None
    for b in row["bookings"]:
        assert b["title"] and b["starts"] and b["ends"], "the itinerary itself is visible"
        assert b["price"] is None and b["currency"] is None


def test_a_host_cannot_get_a_number_out_by_asking_another_endpoint(client, world):
    """The disruption payload is the richest one there is. Break the trip as the
    owner, then read it as the host through every read there is."""
    trip_id = world["trip"]["trip_id"]
    owner, host = world["owner"]["token"], world["members"]["host"]["token"]
    leg = [b for b in world["trip"]["bookings"] if b["kind"] == "flight"][1]
    client.post("/api/delay", headers=as_(owner),
                json={"trip_id": trip_id, "booking_id": leg["id"], "minutes": 400})

    def numbers(value, path="") -> list:
        found = []
        if isinstance(value, dict):
            for k, v in value.items():
                if k in roles.MONEY_KEYS and v is not None:
                    found.append(f"{path}/{k}")
                found += numbers(v, f"{path}/{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                found += numbers(v, f"{path}[{i}]")
        return found

    for path, params in (("/api/itineraries", {}), ("/api/alerts", {"trip": trip_id}),
                         ("/api/state", {"trip": trip_id})):
        body = client.get(path, headers=as_(host), params=params).json()
        assert numbers(body) == [], f"{path} leaked {numbers(body)[:3]}"


def test_finance_sees_every_number_and_cannot_take_a_plan(client, world):
    trip_id = world["trip"]["trip_id"]
    finance = world["members"]["finance"]["token"]
    row = next(t for t in rows(client, finance) if t["trip_id"] == trip_id)
    assert row["total"] and all(b["price"] is not None for b in row["bookings"])

    changed = client.post("/api/permissions", headers=as_(finance),
                          json={"trip_id": trip_id, "auto_limit": 250})
    assert changed.status_code == 200, "approving money is finance's job"
    leg = [b for b in world["trip"]["bookings"] if b["kind"] == "flight"][1]
    refused = client.post("/api/cancel", headers=as_(finance),
                          json={"trip_id": trip_id, "booking_id": leg["id"]})
    assert refused.status_code == 403, "spending it is not"


# --------------------------------------------------------------------------
# who can act
# --------------------------------------------------------------------------


def test_an_assistant_runs_the_trip_and_is_named_in_the_record(client, world):
    trip_id = world["trip"]["trip_id"]
    assistant = world["members"]["assistant"]["token"]
    leg = [b for b in world["trip"]["bookings"] if b["kind"] == "flight"][1]
    outcome = client.post("/api/cancel", headers=as_(assistant),
                          json={"trip_id": trip_id, "booking_id": leg["id"]}).json()
    plan = outcome["plans"][0]
    done = client.post("/api/act", headers=as_(assistant), json={
        "trip_id": trip_id, "booking_id": leg["id"], "plan_key": plan.get("key") or plan["id"]})
    assert done.status_code == 200, done.text
    assert done.json()["by"] == "the assistant"
    row = next(t for t in rows(client, assistant) if t["trip_id"] == trip_id)
    assert row["acted"]["by"] == "the assistant"


def test_an_organiser_cannot_call_the_whole_trip_off(client, world):
    trip_id = world["trip"]["trip_id"]
    organiser = world["members"]["organiser"]["token"]
    assert client.post("/api/abandon", headers=as_(organiser),
                       json={"trip_id": trip_id, "confirm": True}).status_code == 403


def test_a_host_cannot_change_anything(client, world):
    trip_id = world["trip"]["trip_id"]
    host = world["members"]["host"]["token"]
    leg = [b for b in world["trip"]["bookings"] if b["kind"] == "flight"][0]
    for path, body in (("/api/cancel", {"booking_id": leg["id"]}),
                       ("/api/delay", {"booking_id": leg["id"], "minutes": 90}),
                       ("/api/permissions", {"auto_limit": 1}),
                       ("/api/abandon", {"confirm": True}),
                       ("/api/share", {"name": "x", "role": "host"})):
        assert client.post(path, headers=as_(host),
                           json={"trip_id": trip_id, **body}).status_code == 403, path


# --------------------------------------------------------------------------
# letting people in and out
# --------------------------------------------------------------------------


def test_only_the_owner_shares_and_a_link_is_shown_once(client, world):
    trip_id = world["trip"]["trip_id"]
    assistant = world["members"]["assistant"]["token"]
    assert client.post("/api/share", headers=as_(assistant), json={
        "trip_id": trip_id, "name": "x", "role": "host"}).status_code == 403

    given = share(client, world["owner"]["token"], trip_id, "Nadia", "contact")
    assert given["token"] and given["link"].endswith(given["token"])
    mine = next(t for t in rows(client, world["owner"]["token"]) if t["trip_id"] == trip_id)
    nadia = next(m for m in mine["members"] if m["name"] == "Nadia")
    assert "token" not in nadia, "the token is not readable back"


def test_a_role_that_does_not_exist_or_is_owner_is_refused(client, world):
    trip_id = world["trip"]["trip_id"]
    for role in ("owner", "admin", ""):
        assert client.post("/api/share", headers=as_(world["owner"]["token"]), json={
            "trip_id": trip_id, "name": "x", "role": role}).status_code == 422, role


def test_revoking_closes_the_door(client, world):
    trip_id = world["trip"]["trip_id"]
    owner = world["owner"]["token"]
    given = share(client, owner, trip_id, "Temporary", "host")
    assert trip_id in {t["trip_id"] for t in rows(client, given["token"])}

    gone = client.post("/api/revoke", headers=as_(owner),
                       json={"trip_id": trip_id, "person": given["person"]})
    assert gone.status_code == 200
    assert trip_id not in {t["trip_id"] for t in rows(client, given["token"])}
    assert client.get("/api/alerts", headers=as_(given["token"]),
                      params={"trip": trip_id}).status_code == 404


def test_profiles_are_per_person(client, world):
    owner, assistant = world["owner"]["token"], world["members"]["assistant"]["token"]
    client.post("/api/profile", headers=as_(owner), json={"home": "singapore"})
    assert client.get("/api/profile", headers=as_(owner)).json()["profile"]["home"] == "SIN"
    assert client.get("/api/profile", headers=as_(assistant)).json()["profile"]["home"] == ""
    assert client.get("/api/profile").json()["profile"]["home"] == ""
    client.post("/api/profile", headers=as_(owner), json={})
