"""The proposer may widen the search. It may not invent one.

Every test here is about the boundary between what a model is allowed to say
and what only a provider knows. A model that names a place we cannot resolve,
writes a window in the wrong week, or repeats a search that already came back
empty must be refused with a reason it can act on -- and must cost nothing
while being refused.
"""

from datetime import timedelta

import pytest

from demo_trip import TRIP, dt
from domain import Disruption
from plan import recovery_gap
from propose import (HORIZON, MAX_SHIFT, MODES, Budget, Probe, Reason,
                     Rejected, Resolved, check, resolve, search, seed,
                     suggest, terminals, vocabulary)

DISRUPTION = Disruption("sq346", dt(12, 10, 25), "late inbound aircraft", 0.91)


@pytest.fixture
def gap():
    got = recovery_gap(TRIP, DISRUPTION)
    assert got is not None, "the scripted disruption must strand something"
    return got


class FakeModel:
    """Returns whatever body it was given, once per call."""

    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), 0

    def invoke(self, messages):
        self.calls += 1
        body = self.bodies[min(self.calls - 1, len(self.bodies) - 1)]
        return type("Reply", (), {"content": body})()


# ------------------------------------------------------------------ vocabulary

def test_vocabulary_comes_from_places_not_a_second_table():
    vocab = vocabulary()
    assert "ZRH" in vocab["flight"] and "MXP" in vocab["flight"]
    assert "ZRH_HB" in vocab["rail"] and "MILANO_C" in vocab["rail"]
    # An airport with no station must not appear as somewhere to catch a train.
    assert "SIN" not in vocab["rail"]


# ----------------------------------------------------------------- validation

def test_an_unknown_place_is_refused_by_name(gap):
    got = check("flight", "ZRH", "MILANO_X", gap.not_before, gap.by, gap.not_before)
    assert isinstance(got, Rejected)
    assert got.reason is Reason.UNKNOWN_PLACE
    assert "MILANO_X" in got.detail


def test_rail_from_a_city_with_no_station_is_refused_separately(gap):
    """Not the same failure as an unknown place, and the model needs to know
    which: Singapore exists, its platform does not."""
    got = check("rail", "SIN", "MILANO_C", gap.not_before, gap.by, gap.not_before)
    assert isinstance(got, Rejected) and got.reason is Reason.NO_STATION


def test_a_mode_nobody_implemented_is_malformed(gap):
    got = check("ferry", "ZRH", "MXP", gap.not_before, gap.by, gap.not_before)
    assert isinstance(got, Rejected) and got.reason is Reason.MALFORMED


def test_a_window_beyond_the_bound_is_out_of_range(gap):
    got = check("flight", "ZRH", "MXP", gap.not_before + MAX_SHIFT + timedelta(hours=1),
                gap.by + MAX_SHIFT + timedelta(hours=2), gap.not_before)
    assert isinstance(got, Rejected) and got.reason is Reason.OUT_OF_RANGE


def test_a_backwards_window_is_refused(gap):
    got = check("flight", "ZRH", "MXP", gap.by, gap.not_before, gap.not_before)
    assert isinstance(got, Rejected) and got.reason is Reason.EMPTY_WINDOW


def test_the_same_search_minutes_apart_is_refused_as_a_duplicate(gap):
    """Two probes eleven minutes apart are one search, and paying a provider
    twice to learn that is how a budget disappears."""
    model = FakeModel('{"probes":[{"mode":"rail","origin":"ZRH_HB",'
                      '"destination":"MILANO_C","after_min":0},'
                      '{"mode":"rail","origin":"ZRH_HB",'
                      '"destination":"MILANO_C","after_min":11}]}')
    probes, refused = suggest(gap, model=model)
    assert len(probes) == 1
    assert [r.reason for r in refused] == [Reason.DUPLICATE]


# ----------------------------------------------------------------- the floor

def test_the_gap_aims_at_a_place_no_provider_sells(gap):
    """The premise of the whole module. `recovery_gap` targets the first
    booking that falls out of reach -- a church with a fresco, not an airport.
    A searcher that assumed the gap named a terminal would find nothing."""
    assert gap.destination == "SMG"
    assert terminals("flight", "SMG") and terminals("rail", "SMG")


def test_seed_derives_the_two_searches_replan_hard_codes(gap):
    probes = seed(gap)
    assert {p.mode for p in probes} == set(MODES)
    routes = {(p.mode, p.origin, p.destination) for p in probes}
    assert ("flight", "ZRH", "MXP") in routes
    assert ("rail", "ZRH_HB", "MILANO_C") in routes


def test_the_window_opens_after_the_walk_to_the_platform(gap):
    """A traveller landing at 10:25 is not on a platform at 10:25."""
    rail = next(p for p in seed(gap) if p.origin == "ZRH_HB")
    assert rail.not_before > gap.not_before


def test_the_search_outlives_the_deadline_it_was_derived_from(gap):
    """Cutting the search at the museum slot would delete Plan A, which lands
    at 16:35, gives the slot up and rescues the rest of the trip."""
    for probe in seed(gap):
        assert probe.by > gap.by
        assert probe.by <= gap.by + HORIZON


def test_no_model_falls_all_the_way_back_to_the_floor(gap):
    probes, refused = suggest(gap, model=None)
    assert [p.key for p in probes] == [p.key for p in seed(gap)]
    assert refused == []


def test_prose_instead_of_json_falls_back_rather_than_raising(gap):
    probes, _ = suggest(gap, model=FakeModel("I would try the train, I think."))
    assert [p.key for p in probes] == [p.key for p in seed(gap)]


def test_the_fallback_never_repeats_a_search_already_run(gap):
    """An unhelpful model must not become an infinite one."""
    already = {p.key for p in seed(gap)}
    probes, _ = suggest(gap, model=None, tried=already)
    assert probes == []


# ------------------------------------------------------------ offsets, not dates

def test_offsets_are_resolved_against_the_gap_not_the_model(gap):
    model = FakeModel('{"probes":[{"mode":"rail","origin":"ZRH_HB",'
                      '"destination":"MILANO_C","after_min":90,"until_min":0,'
                      '"why":"later train"}]}')
    probes, _ = suggest(gap, model=model)
    assert probes[0].not_before == gap.not_before + timedelta(minutes=90)
    assert probes[0].by == gap.by
    # The day is the gap's, whatever the model believed the date to be.
    assert probes[0].not_before.date() == gap.not_before.date()


def test_a_non_numeric_offset_is_refused_without_calling_a_provider(gap):
    model = FakeModel('{"probes":[{"mode":"rail","origin":"ZRH_HB",'
                      '"destination":"MILANO_C","after_min":"soonish"}]}')
    budget = Budget()
    probes, refused = suggest(gap, model=model, budget=budget)
    assert any(r.reason is Reason.MALFORMED for r in refused)
    assert budget.spent["calls"] == 0


# --------------------------------------------------------------------- budget

def test_the_loop_is_bounded_by_the_counter_not_the_model(gap):
    """A model that answers forever must still stop."""
    model = FakeModel('{"probes":[{"mode":"flight","origin":"ZRH",'
                      '"destination":"FCO","after_min":0,"until_min":600}]}')
    budget = Budget(rounds=2, probes=4, calls=4)
    search(gap, model=model, budget=budget)
    assert budget.spent["rounds"] <= 2
    assert budget.spent["calls"] <= 4


def test_a_spent_budget_refuses_rather_than_silently_returning_nothing(gap):
    budget = Budget(rounds=0)
    probes, refused = suggest(gap, model=None, budget=budget)
    assert probes == []
    assert refused[0].reason is Reason.OUT_OF_BUDGET


def test_resolve_will_not_call_a_provider_it_cannot_pay_for(gap):
    budget = Budget(calls=0)
    got = resolve(seed(gap)[0], budget)
    assert isinstance(got, Rejected) and got.reason is Reason.OUT_OF_BUDGET


# -------------------------------------------------------------------- resolving

def test_terminals_are_nearest_first(gap):
    got = terminals("flight", "SMG")
    assert [t[0] for t in got][:1] == ["MXP"]
    assert got == sorted(got, key=lambda pair: pair[1])


def test_an_unrecorded_route_is_named_never_substituted(gap):
    """The one failure that would put a wrong number on stage: a probe for a
    route nobody recorded must not quietly resolve to a different search."""
    probe = check("flight", "MAD", "BCN", gap.not_before, gap.by, gap.not_before)
    got = resolve(probe, Budget())
    assert isinstance(got, Rejected)
    assert got.reason in (Reason.PORT_UNAVAILABLE, Reason.NO_OFFERS)


def test_a_resolved_probe_returns_offers_inside_its_own_window(gap):
    got = resolve(seed(gap)[0], Budget())
    if isinstance(got, Rejected):
        pytest.skip(f"no recording for the seeded search: {got.reason.value}")
    assert isinstance(got, Resolved) and got.offers
    for offer in got.offers:
        assert got.probe.not_before <= offer.depart
        assert offer.arrive <= got.probe.by


def test_search_returns_offers_and_the_reasons_it_refused(gap):
    offers, refused = search(gap, model=None, budget=Budget())
    assert isinstance(offers, list) and isinstance(refused, list)
    assert all(isinstance(r, Rejected) for r in refused)
    ids = [o.id for o in offers]
    assert len(ids) == len(set(ids)), "the same offer must not be ranked twice"


def test_every_refusal_carries_a_hint_a_prompt_can_use(gap):
    _, refused = search(gap, model=FakeModel('{"probes":[{"mode":"rail",'
                                             '"origin":"SIN","destination":"MILANO_C"}]}'),
                        budget=Budget(rounds=1))
    assert refused
    for r in refused:
        assert r.hint.strip() and r.reason.value in r.hint
