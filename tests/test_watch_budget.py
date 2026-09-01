"""The status feed's month cannot be spent by one demo morning.

600 units a month at 54 an hour is gone by lunch. The meter gives the live
path a daily allowance and, when it is spent, raises the same error shape a
network failure would -- so `base.call` answers from a recording and marks the
port degraded instead of the quota dying silently mid-demo.
"""

from __future__ import annotations

import pytest

import aerodatabox


@pytest.fixture(autouse=True)
def _fresh_meter():
    aerodatabox._spent.clear()
    yield
    aerodatabox._spent.clear()


def test_the_meter_counts_and_then_refuses(monkeypatch):
    monkeypatch.setenv("DOWNSTREAM_ADB_DAILY_BUDGET", "2")
    calls = []
    monkeypatch.setattr(aerodatabox, "get_json",
                        lambda url, headers=None: calls.append(url) or [])
    aerodatabox._metered_get("https://x/one")
    aerodatabox._metered_get("https://x/two")
    assert len(calls) == 2
    with pytest.raises(TimeoutError) as caught:
        aerodatabox._metered_get("https://x/three")
    assert "budget" in str(caught.value)
    assert len(calls) == 2, "the refused call must not reach the network"


def test_exhaustion_is_the_error_shape_base_call_already_survives():
    """base.call catches (URLError, TimeoutError, OSError, ValueError) and
    falls back to the recording, marking the port degraded. The meter must
    raise inside that net -- a new exception type would escape it and turn
    a spent budget into a crashed watch pass."""
    import base
    import inspect
    src = inspect.getsource(base.call)
    assert "TimeoutError" in src, "the fallback net this meter relies on moved"
