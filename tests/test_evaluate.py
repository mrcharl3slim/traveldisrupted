"""The decision harness, and the one property that makes it worth running.

A harness that cannot fail is a decoration. So this asserts both halves: every
case decides as expected today, AND a broken engine is actually noticed.
"""

import evaluate
import permit
import pytest


def test_every_case_decides_as_expected():
    """The table on the evaluation slide, asserted rather than screenshotted."""
    failures = []
    for case in evaluate.CASES:
        got, why = case.run()
        if got != case.expected:
            failures.append(f"{case.id}: expected {case.expected!r}, got {got!r} ({why})")
    assert not failures, "\n".join(failures)


def test_the_harness_notices_a_broken_permission_gate(monkeypatch):
    """The mutation that matters most: an engine that ignores the traveller's
    rules -- the kill switch, the caps, never-book -- must not come back green.
    Without this, a harness of twenty passing cases proves only that it ran."""
    import flow

    original = permit.judge

    def ignores_the_rules(plan, perms):
        return original(plan, permit.Permissions(auto_limit=perms.auto_limit or 5000.0))

    monkeypatch.setattr(permit, "judge", ignores_the_rules)
    monkeypatch.setattr(flow.permit, "judge", ignores_the_rules)

    caught = [c.id for c in evaluate.CASES if c.run()[0] != c.expected]
    assert len(caught) >= 4, (
        "a permission engine that ignores disarm, caps and never-book was "
        f"noticed by only {len(caught)} case(s): {caught}")


def test_every_case_is_distinct_and_labelled():
    ids = [c.id for c in evaluate.CASES]
    assert len(ids) == len(set(ids)), "two cases share an id"
    assert len(evaluate.CASES) >= 20, "the deck claims twenty cases"
    for c in evaluate.CASES:
        assert c.scenario and c.expected, f"{c.id} is missing its scenario or expectation"


@pytest.mark.parametrize("argv", [[], ["--verbose"], ["--json"]])
def test_the_report_runs_green_in_every_mode(argv, capsys):
    assert evaluate.main(argv) == 0
    assert capsys.readouterr().out.strip()
