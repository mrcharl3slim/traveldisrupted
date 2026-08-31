"""The engine as somebody else's building block.

These test the tool functions rather than the stdio transport: the transport is
the MCP library's problem, and what matters here is that an agent calling these
gets numbers anchored to the right dates and caveats it cannot skip.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import mcp_server
from demo_trip import DEFAULT_BASE, resolve_base


# -- date resolution ------------------------------------------------------

def test_default_is_today_not_the_written_scenario():
    """Live flight status covers about a week either side of now, so a trip
    pinned to October cannot be shown live in September. Today is the norm and
    "as written" is the special case."""
    assert resolve_base() == date.today()
    assert resolve_base("") == date.today()
    assert resolve_base("today") == date.today()


def test_written_reaches_the_original_literal():
    assert resolve_base("written") is None


def test_offsets_and_explicit_dates():
    assert resolve_base("+3") == date.today() + timedelta(days=3)
    assert resolve_base("-1") == date.today() - timedelta(days=1)
    assert resolve_base("2026-09-07") == date(2026, 9, 7)


def test_nonsense_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        resolve_base("next tuesday")


# -- the tools ------------------------------------------------------------

def test_itinerary_shows_the_two_ticket_groups():
    """The whole scenario is that the Singapore and Zurich legs are separate
    contracts, so nobody owes a reaccommodation. An agent reasoning about this
    trip has to be able to see that."""
    bookings = {b["id"]: b for b in mcp_server.itinerary()["bookings"]}
    assert bookings["sq346"]["ticket_group"] != bookings["lx1608"]["ticket_group"]


def test_impact_is_anchored_to_the_requested_dates():
    today = mcp_server.impact_of(base="today")
    written = mcp_server.impact_of(base="written")
    assert today["anchored_to"] == date.today().isoformat()
    assert written["anchored_to"] == DEFAULT_BASE.isoformat()
    assert today["do_nothing_sgd"] == written["do_nothing_sgd"] == 423.0


def test_plans_rank_the_same_through_the_tool_as_through_the_engine():
    plans = mcp_server.recovery_plans(base="written")["plans"]
    assert plans[0]["net_cash_sgd"] == -21.0
    assert plans[0]["total_damage_sgd"] < plans[-1]["total_damage_sgd"]


def test_every_action_carries_a_lane_an_agent_must_respect():
    """"call" means no consumer API exists. An agent that reports a call-lane
    action as done has lied to a stranded traveller."""
    plans = mcp_server.recovery_plans()["plans"]
    lanes = {a["lane"] for p in plans for a in p["actions"]}
    assert lanes <= {"auto", "tap", "call"}
    assert "call" in lanes


def test_an_estimated_fare_is_labelled_in_the_tool_output():
    """The rail fare is the one number no reachable API quotes. An agent
    repeating it downstream must be able to tell."""
    plans = mcp_server.recovery_plans()["plans"]
    sources = {a["price_source"] for p in plans for a in p["actions"]}
    assert "estimate" in sources
