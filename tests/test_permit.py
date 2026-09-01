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
    assert "within your S$" in verdict.why
    kinds = {kind for kind, _ in verdict.approvals.values()}
    assert kinds <= {permit.AUTO, permit.PRE_AUTHORISED}


def test_a_plan_over_the_cap_waits_and_says_by_how_much(plans):
    best = plans[0]
    verdict = permit.judge(best, permit.Permissions(auto_limit=best.cash_out - 1))
    assert verdict.allowed and not verdict.auto
    assert "exceeds your S$" in verdict.why


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


def _plan_costing(cash: float):
    from plan import Action, Lane, Plan
    return Plan(id="x", name="p", tagline="", actions=[
        Action(verb="notify", label="Email the hotel", lane=Lane.AUTO),
        Action(verb="buy", label="Book it", lane=Lane.TAP, cash_out=cash)],
        arrives_at=None, arrives_where=None)


def test_the_tiers_at_every_boundary():
    """The deck's numbers, reading (a) -- the only one where both do work:
    <=300 auto, 301-500 auto-hold, >500 explicit authorization. Table-driven
    at 299/300/301/499/500/501 as the acceptance demands."""
    rules = permit.Permissions(auto_limit=300, hold_limit=500)
    table = [(299, permit.PRE_AUTHORISED, True, False),
             (300, permit.PRE_AUTHORISED, True, False),
             (301, permit.AUTO_HELD, False, True),
             (499, permit.AUTO_HELD, False, True),
             (500, permit.AUTO_HELD, False, True),
             (501, permit.NEEDS_APPROVAL, False, False)]
    for cash, state, auto, hold in table:
        plan = _plan_costing(cash)
        verdict = permit.judge(plan, rules)
        assert verdict.approval(plan.actions[1])[0] == state, cash
        assert (verdict.auto, verdict.hold) == (auto, hold), cash
    over = permit.judge(_plan_costing(501), rules)
    assert "exceeds your S$500" in over.why, "the refusal names the outer limit"


def test_the_tiers_are_configured_not_shipped():
    """The default is 0/0: a product that spends money out of the box has
    shipped the demo persona's settings as everyone's."""
    verdict = permit.judge(_plan_costing(1), permit.Permissions())
    assert not verdict.auto and not verdict.hold


def test_a_hold_ceiling_below_the_cap_is_a_contradiction_not_a_config():
    fixed = permit.Permissions.from_dict({"auto_limit": 300, "hold_limit": 100})
    assert fixed.hold_limit == 300, "the auto tier wins; the middle tier is empty"
    assert permit.Permissions.from_dict({"hold_limit": "lots"}).hold_limit == 0.0


def test_the_kill_switch_beats_the_tiers():
    off = permit.Permissions(auto_limit=300, hold_limit=500, disarmed=True)
    for cash in (100, 400, 900):
        verdict = permit.judge(_plan_costing(cash), off)
        assert not verdict.auto and not verdict.hold
        assert verdict.why == permit.DISARMED


def test_the_kill_switch_beats_every_other_rule(plans):
    """The acceptance, verbatim: disarmed, no plan yields AUTO or
    PRE_AUTHORISED for any action -- the cap ignored, the AUTO-lane email
    included -- with the one fixed reason. Fixed on purpose: a switch that
    explains itself differently per action invites arguing with it."""
    rules = permit.Permissions(auto_limit=1_000_000, disarmed=True)
    for plan in plans:
        verdict = permit.judge(plan, rules)
        assert verdict.allowed and not verdict.auto, "read-only still means read"
        for state, why in verdict.approvals.values():
            assert state == permit.NEEDS_APPROVAL
            assert why == permit.DISARMED


def test_re_arming_restores_the_rules_as_they_were(plans):
    best = plans[0]
    armed = permit.Permissions(auto_limit=best.cash_out + 1)
    off = permit.Permissions(auto_limit=best.cash_out + 1, disarmed=True)
    assert permit.judge(best, armed).auto
    assert not permit.judge(best, off).auto
    assert permit.judge(best, armed).auto, "the flag is state, not a ratchet"


def test_rules_survive_a_round_trip_and_tolerate_garbage():
    rules = permit.Permissions(auto_limit=250, never=("Rail", " ryanair "),
                               always_ask=("cancel",))
    back = permit.Permissions.from_dict(rules.to_dict())
    assert back.auto_limit == 250
    assert permit.Permissions.from_dict(
        permit.Permissions(disarmed=True).to_dict()).disarmed is True
    assert back.never == ("rail", "ryanair"), "normalised on the way in"
    assert back.always_ask == ("cancel",)

    # A stored trip from before this file, and a form that sent nonsense.
    assert permit.Permissions.from_dict(None) == permit.Permissions()
    assert permit.Permissions.from_dict({"auto_limit": "lots", "never": "rail, "}) \
        == permit.Permissions(never=("rail",))
    assert permit.Permissions.from_dict({"auto_limit": -5}).auto_limit == 0.0


def test_always_ask_reaches_the_auto_lane_email(plans):
    """The one AUTO verb that leaves the building. always_ask("notify") used
    to be a dead setting: the AUTO shortcut ran first and waved the email
    through unexamined."""
    best = plans[0]
    emails = [a for a in best.actions if a.verb == "notify"]
    assert emails, "the premise: the best plan emails somebody"
    verdict = permit.judge(best, permit.Permissions(
        auto_limit=best.cash_out + 1, always_ask=("notify",)))
    assert not verdict.auto, "a plan whose email needs asking is not auto"
    kind, why = verdict.approvals[emails[0].label]
    assert kind == permit.NEEDS_APPROVAL and "notify" in why
