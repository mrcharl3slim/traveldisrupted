"""The graph, end to end, with no keys and no network.

A fake model stands in for Bedrock. That is not a testing shortcut -- it is the
same posture as the ports: if the pipeline cannot be exercised without
credentials, it cannot be trusted to survive losing them mid-demo.
"""

from datetime import datetime, timedelta

import pytest

from agent import GRAPH, _template
from demo_trip import TRIP, CEST, dt
from parse import parse_policy
from plan import Lane

NOW = datetime(2026, 10, 12, 2, 38, tzinfo=CEST)


class FakeReply:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """Records what it was asked and returns what it was told to."""

    def __init__(self, content):
        self.content, self.calls = content, []

    def invoke(self, messages):
        self.calls.append(messages)
        return FakeReply(self.content)


class DeadModel:
    def invoke(self, messages):
        raise RuntimeError("bedrock: ThrottlingException")


def run(model=None):
    return GRAPH.invoke({
        "trip": TRIP, "now": NOW, "flight": "SQ346", "flight_day": dt(11, 9, 0),
        "booking_id": "sq346", "search_day": dt(12, 9, 0), "model": model})


def test_the_graph_reaches_the_same_numbers():
    out = run()
    assert out["impact"].do_nothing_cost == 282.0
    assert out["chosen"].net_cash == -14.0
    assert out["chosen"].total_damage == 176.0


def test_an_on_time_flight_stops_at_detection(monkeypatch):
    """Most days nothing is wrong. Those days must cost nothing downstream."""
    import agent
    monkeypatch.setattr(agent.aerodatabox, "disruption", lambda *a, **k: None)
    out = run()
    assert "impact" not in out and "plans" not in out


def test_the_lanes_are_grouped_for_the_handoff():
    out = run()
    lanes = out["lanes"]
    assert [a.booking_id for a in lanes[Lane.CALL.value]] == ["lx1608"]
    assert all(a.cash_out == 0 and a.cash_in == 0 for a in lanes[Lane.AUTO.value])


def test_without_a_model_the_sentence_is_still_good():
    """The fallback has to be publishable, not a placeholder.

    The buffer is read off the chosen plan rather than typed in here. It is a
    property of the recording, and pinning the number meant the test failed
    the day a different train won -- which said nothing about the sentence.
    """
    out = run()
    r = out["rationale"]
    assert "back in your pocket" in r and "282" not in r
    _bid, buf = out["chosen"].tightest
    assert f"{int(buf.total_seconds() // 60)} minutes to spare" in r


def test_a_dead_model_costs_prose_never_the_plan():
    out = run(DeadModel())
    assert out["chosen"].net_cash == -14.0
    assert out["rationale"] == _template(out)


def test_the_model_is_told_the_fare_is_an_estimate():
    """The rail fare is the one figure no API quotes. The model must never be
    handed it as though it were quoted."""
    m = FakeModel("Take the 11:33 train.")
    run(m)
    sent = str(m.calls[0][-1]["content"])
    assert "estimated_fares" in sent
    assert "ESTIMATE" in sent


def test_policy_parsing_falls_back_to_non_refundable():
    """No model, no invented refunds."""
    p = parse_policy("Free change up to 4 hours before pickup", dt(12, 10, 0))
    assert p.windows == [] and p.recoverable_at(NOW) == 0.0
    assert p.source


def test_the_model_may_not_emit_absolute_dates():
    """Offsets only, resolved here against the booking's own times. A model
    allowed to write '2026-10-12T06:00' can write the wrong day and nothing
    downstream would ever know."""
    m = FakeModel('{"windows": [{"offset_hours_before": 4, "anchor": "start", '
                  '"refund": 48, "fee": 0, "label": "free change 4h before"}]}')
    p = parse_policy("Free change up to 4 hours before pickup",
                     dt(12, 10, 0), model=m)
    assert p.windows[0].closes == dt(12, 6, 0)
    assert "NEVER output absolute dates" in m.calls[0][0]["content"]


def test_garbage_from_the_model_is_not_a_refund():
    p = parse_policy("Non-refundable", dt(12, 16, 0),
                     model=FakeModel("sure! here you go"))
    assert p.windows == []


def test_the_mcp_tools_return_the_same_arithmetic():
    """The MCP surface is a view of the engine, not a second implementation."""
    import mcp_server
    imp = mcp_server.impact_of()
    assert imp["do_nothing_eur"] == 282.0 and imp["recoverable_eur"] == 160.0
    assert imp["next_cutoff"]["booking"].startswith("Malpensa")
    best = mcp_server.recovery_plans()["plans"][0]
    assert best["net_cash_eur"] == -14.0 and best["total_damage_eur"] == 176.0
