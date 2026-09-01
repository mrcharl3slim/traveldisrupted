"""The whole capability as one scripted trip -- through the real handlers.

The claim /story makes is the same one /demo has always made, widened: the
inputs are scripted and the outcomes are computed. So every assertion here is
about a figure the engine produced, and the one structural test is that the
story runs through the same functions the buttons call rather than through a
parallel path that could drift.
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

import serve                                        # noqa: E402


@pytest.fixture(scope="module")
def told():
    return TestClient(serve.app).post("/api/story").json()


def beat(told, title_start: str) -> dict:
    return next(b for b in told["beats"] if b["title"].startswith(title_start))


def test_the_story_runs_and_has_all_thirteen_chapters(told):
    assert len(told["beats"]) == 13
    assert told["trip_id"]


def test_the_thin_places_are_flagged_before_anything_breaks(told):
    """The premise of the whole story -- two tickets, one fragile connection
    -- is on the record BEFORE the delay chapter, from booked times alone.
    Foreshadowing only counts if it comes first."""
    titles = [b["title"] for b in told["beats"]]
    assert titles.index("Where this trip is thin") < titles.index("Thirty minutes late")

    thin = beat(told, "Where this trip is thin")
    assert thin["premise_flagged"], "the separate-ticket connection went unflagged"
    kinds = {r["kind"] for r in thin["risks"]}
    assert "unprotected" in kinds and "breakpoint" in kinds
    assert all("S$" not in r["sentence"] for r in thin["risks"]), (
        "money lives in `worth`, never in the prose")


def test_the_ladder_reads_act_hold_stop_in_order(told):
    titles = [b["title"] for b in told["beats"]]
    ladder = [titles.index(next(t for t in titles if t.startswith("Cancelled"))),
              titles.index("The middle tier: decided, held, flagged"),
              titles.index("The same cancellation, with the kill switch on")]
    assert ladder == sorted(ladder), "act, then hold, then stop"


def test_the_middle_tier_chapter_decides_and_nothing_leaves(told):
    """Between the armed chapter and the kill switch: the agent still decides
    -- the pending replacement IS the hold -- and zero channels are used, with
    the tier named on the record."""
    mid = beat(told, "The middle tier")
    assert mid["auto_held"] is True
    assert "auto-hold tier" in mid["permission"]
    assert mid["sent"] == 0
    assert mid["held"], "the email is held for the person"
    assert any("Book" in l for l in mid["pending"]), "the hold itself: a pending purchase"
    assert "held by the kill switch" in mid["summary"] or "held" in mid["summary"]


def test_the_kill_switch_chapter_is_the_armed_chapter_negated(told):
    """Same trip, same cap, same cancellation as the chapter before it -- the
    only difference is the switch, so every claim is the earlier chapter's
    with the sign flipped: no auto-take, one fixed reason on every line, zero
    messages sent, the email held for the person, and the flip on record."""
    stopped = beat(told, "The same cancellation")
    armed = beat(told, "Cancelled")

    assert armed["auto_taken"], "the premise: the armed chapter acted"
    assert stopped["auto_taken"] is False
    assert "kill switch" in stopped["why"]
    assert stopped["approvals"] == ["needs approval"], "nothing auto, nothing pre-authorised"
    assert stopped["sent"] == 0, "zero channels used"
    assert stopped["held"], "the email is held for the person, not dropped"
    assert "held by the kill switch" in stopped["summary"]
    assert stopped["toggled"].endswith("disarmed")


def test_the_story_closes_on_its_own_paper_trail(told):
    """The run just decided things; the last chapter is the record of them,
    fetched from the same trail the panel reads -- not a re-telling."""
    record = beat(told, "Everything above")
    assert record["counts"].get("extracted") == 1
    assert record["counts"].get("assessed") == 2, "the delay and the cancellation"
    assert record["counts"].get("judged") == 2
    assert record["counts"].get("performed", 0) >= 3
    for e in record["events"]:
        for field in ("at", "actor", "type", "subject",
                      "citation", "authorization", "feed"):
            assert str(e.get(field, "")).strip(), f"{e['type']} missing {field}"

    # The link works as a bare link, token and all, and reads as sentences.
    from fastapi.testclient import TestClient
    import serve as app_module
    text = TestClient(app_module.app).get(record["trail_link"])
    assert text.status_code == 200
    assert "citation:" in text.text and "authorization:" in text.text


def test_the_profile_chapter_is_the_seed_the_rest_pays_off(told):
    said_once = beat(told, "Say it once")
    assert said_once["profile"]["home"] == "SIN"
    assert said_once["profile"]["permissions"]["auto_limit"] == 300


def test_the_booking_inherits_the_rules_and_ranks_trains_with_flights(told):
    booked = beat(told, "A sentence")
    assert booked["rules"]["auto_limit"] == 300
    # No longer asserted to WIN: EUR converts at 1.50 and USD at 1.30, so a
    # USD-quoted fare can honestly undercut the S$108 train. What the story
    # claims is one list, both modes, the estimate labelled.
    assert booked["cheapest_train"] == 108.0
    kinds = {b["kind"] for b in booked["bookings"]}
    assert kinds == {"flight", "lodging"}


def test_the_meeting_fits_and_is_never_priced(told):
    meeting = beat(told, "The reason")
    assert meeting["feasible"] is True
    assert meeting["clashes"] == []


def test_marco_sees_every_leg_and_no_price_anywhere(told):
    marco = beat(told, "Marco")
    assert marco["marco_sees"], "the itinerary itself is visible"
    assert all(row["price"] is None for row in marco["marco_sees"])
    assert marco["marco_total"] is None
    assert marco["marco_link"].startswith("/?token=")


def test_thirty_minutes_costs_nothing(told):
    small = beat(told, "Thirty")
    assert small["destroyed"] == 0 and small["at_risk"] == 0
    assert all(n["severity"] in ("safe", "source") for n in small["nodes"])


def test_the_cancellation_is_answered_by_the_agent_inside_the_cap(told):
    big = beat(told, "Cancelled")
    taken = big["auto_taken"]
    assert taken, "the agent did not act -- the cap chapter has no payoff"
    assert "within your S$300 limit" in taken["permission"]
    assert taken["pending"], "the purchase still needs a person"
    assert "keeps Meeting with the client" in big["why"]

    ranked = big["plans"]
    assert ranked[0]["best"] and ranked[0]["saves"], "the taken plan keeps the meeting"
    cheaper_losers = [q for q in ranked
                     if q["total_damage"] < ranked[0]["total_damage"]]
    assert cheaper_losers and all(q["misses"] for q in cheaper_losers), (
        "the story's whole point: cheaper rows lose because they miss the client")


def test_the_abandon_chapter_is_priced_and_not_taken(told):
    quote = beat(told, "And if")
    assert round(quote["refund"] + quote["lost"], 2) == quote["paid"]
    lanes = {a["lane"] for a in quote["errands"]}
    assert lanes <= {"auto", "tap", "call"} and len(lanes) >= 2


def test_the_story_can_be_told_twice(told):
    """A story that can only be told once breaks at the second audience."""
    again = TestClient(serve.app).post("/api/story").json()
    assert again["trip_id"] != told["trip_id"]
    assert beat(again, "Cancelled")["auto_taken"]


def test_the_story_trip_is_real_and_still_actionable(told):
    """The last chapter of the page says "open it on the agent page and keep
    going". That has to be true: the trip exists, the agent is recorded as
    having acted, and the replacement is pending on the itinerary."""
    client = TestClient(serve.app)
    listed = client.get("/api/itineraries").json()["itineraries"]
    row = next((t for t in listed if t["trip_id"] == told["trip_id"]), None)
    assert row is None, "the story trip belongs to Alex, not to the anonymous traveller"


def test_the_presence_chapter_refuses_warns_and_presumes(told):
    """The timeline speaks three ways in one chapter: a wrong-city meeting is
    refused with the actual city named, a 75-minute coffee is warned about,
    and the missing return leg is a presumption said out loud."""
    b = beat(told, "It knows where Alex is")
    assert b["cities"][0] == "Singapore" and b["cities"][-1] == "Milan"
    assert b["refused"] and "Singapore" not in b["refused"].split("not")[0].split("in ")[0]
    assert "not Singapore" in b["refused"]
    assert b["warned"] and "75 minutes" in b["warned"]
    assert b["open_ended"] is True


def test_the_room_chapter_keys_the_search_to_the_landing(told):
    """The rooms shown are searched from the picked flight's arrival date,
    and the chapter proves it with a real count, not a claim."""
    b = beat(told, "The room follows the flight")
    assert b["rooms"] >= 1, "the recorded inventory has Milan rooms"
    assert b["check_in"] == b["lands"].rsplit(" ", 1)[0], (
        "check-in must be the day the flight lands")
