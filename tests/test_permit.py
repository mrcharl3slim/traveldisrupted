"""What the agent may do on its own, as the traveller defined it.

Two questions decide whether an action happens without a click. CAN we is
`plan.Lane`, a fact about the supplier, and has tests of its own. MAY we is
this file, a fact about the person -- and the property every test here guards
is that nothing becomes autonomous by omission.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import permit
from demo_trip import CEST, TRIP, dt
from domain import Disruption
from offers import build_offers
from plan import generate

NOW = datetime(2026, 10, 12, 2, 38, tzinfo=CEST)
DISRUPTION = Disruption("sq346", dt(12, 10, 25), "late inbound aircraft", 0.91)


@pytest.fixture(scope="module")
def plans():
    return generate(TRIP, DISRUPTION, NOW, build_offers(None))


def test_nothing_is_pre_authorised_by_default(plans):
    """The default rules have to reproduce the behaviour before this file
    existed: every plan that costs money waits for a person."""
    best = plans[0]
    assert best.cash_out > 0, "the premise: the best plan buys something"
    verdict = permit.judge(best, permit.Permissions())
    assert verdict.allowed and not verdict.auto
    assert "not pre-authorised" in verdict.why


def test_a_plan_within_the_cap_may_be_taken_without_asking(plans):
    best = plans[0]
    verdict = permit.judge(best, permit.Permissions(auto_limit=best.cash_out + 1))
    assert verdict.auto
    assert "within your EUR" in verdict.why
    kinds = {kind for kind, _ in verdict.approvals.values()}
    assert kinds <= {permit.AUTO, permit.PRE_AUTHORISED}


def test_a_plan_over_the_cap_waits_and_says_by_how_much(plans):
    best = plans[0]
    verdict = permit.judge(best, permit.Permissions(auto_limit=best.cash_out - 1))
    assert verdict.allowed and not verdict.auto
    assert "exceeds your EUR" in verdict.why


def test_inaction_is_never_taken(plans):
    """It is what happens when nothing is. A cap high enough to cover every
    plan must not turn "do nothing" into an action the agent performs."""
    noop = next(p for p in plans if p.id == "noop")
    assert not permit.judge(noop, permit.Permissions(auto_limit=10_000)).auto


def test_never_rules_out_the_mode_or_the_carrier_the_agent_would_choose(plans):
    """"Never rail" removes the train from the recommendation. It must not
    remove the phone call to a hotel the traveller already holds -- the rule
    is about choices, not about the trip as booked."""
    rail = next(p for p in plans if p.mode == "rail")
    flight = next(p for p in plans if p.mode == "flight")
    rules = permit.Permissions(never=("rail",))

    assert not permit.judge(rail, rules).allowed
    assert permit.judge(rail, rules).why == "you said never rail"
    assert permit.judge(flight, rules).allowed
    assert permit.judge(next(p for p in plans if p.id == "noop"), rules).allowed

    keep, out = permit.permitted(plans, rules)
    assert out and all(p.mode == "rail" for p in out)
    assert all(p.mode != "rail" for p in keep)


def test_never_matches_a_carrier_by_name_case_insensitively(plans):
    flight = next(p for p in plans if p.mode == "flight")
    carrier = next(a.provider for a in flight.actions if a.verb == "buy")
    assert carrier, "the premise: a purchase names who it is from"
    assert not permit.judge(flight, permit.Permissions(never=(carrier.upper(),))).allowed


def test_always_ask_overrides_the_cap(plans):
    """A verb on the always-ask list needs a person however cheap it is."""
    best = plans[0]
    rules = permit.Permissions(auto_limit=10_000, always_ask=("buy",))
    verdict = permit.judge(best, rules)
    assert verdict.allowed and not verdict.auto
    assert "asked before any buy" in verdict.why


def test_the_auto_lane_never_needs_approval(plans):
    """An email to the property moves no money and cannot be undone by not
    sending it later. Asking permission for it is noise that trains people to
    click through the ones that matter."""
    best = plans[0]
    verdict = permit.judge(best, permit.Permissions())
    from plan import Lane
    for a in best.lane(Lane.AUTO):
        assert verdict.approval(a)[0] == permit.AUTO


def test_rules_survive_a_round_trip_and_tolerate_garbage():
    rules = permit.Permissions(auto_limit=250, never=("Rail", " ryanair "),
                               always_ask=("cancel",))
    back = permit.Permissions.from_dict(rules.to_dict())
    assert back.auto_limit == 250
    assert back.never == ("rail", "ryanair"), "normalised on the way in"
    assert back.always_ask == ("cancel",)

    # A stored trip from before this file, and a form that sent nonsense.
    assert permit.Permissions.from_dict(None) == permit.Permissions()
    assert permit.Permissions.from_dict({"auto_limit": "lots", "never": "rail, "}) \
        == permit.Permissions(never=("rail",))
    assert permit.Permissions.from_dict({"auto_limit": -5}).auto_limit == 0.0
