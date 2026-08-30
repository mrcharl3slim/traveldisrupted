"""Where trips live between requests, and what happens when they cannot.

The Postgres half of this had no tests at all and `psycopg` was not even
installed, which is a strange place to be for the code that decides whether a
traveller's itinerary is still there tomorrow. The durable tests run against a
real database when DATABASE_URL names one and skip when it does not; the
degradation test runs everywhere, because that is the path that took the whole
app down and it must not come back.
"""

from __future__ import annotations

import os

import pytest

import store as store_mod

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no DATABASE_URL to test against")


@pytest.fixture
def fresh(monkeypatch):
    """A store built from scratch, because `store()` caches one per process."""
    def build(url: str | None):
        monkeypatch.setattr(store_mod, "_store", None)
        monkeypatch.setattr(store_mod, "unavailable", "")
        if url is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", url)
        return store_mod.store()
    return build


# --------------------------------------------------------------------------
# the failure that mattered
# --------------------------------------------------------------------------


def test_a_database_that_cannot_be_reached_does_not_take_the_app_with_it(fresh):
    """`store()` is on the path of every request that touches a trip, and it
    used to raise. A database that was merely asleep -- the normal state of a
    free instance -- turned the whole app into a 500 rather than into a
    slightly less durable one.

    Losing restarts is bad and losing the app is worse: the first is
    recoverable by reading /health, the second looks like broken software.
    """
    built = fresh("postgresql://nobody:nothing@127.0.0.1:59999/absent")
    assert built.durable is False, "claimed durability it does not have"
    assert store_mod.unavailable, "fell back and said nothing about it"


def test_the_fallback_is_loud_and_names_the_driver_s_own_words(fresh):
    """"Storage unavailable" sends you to the wrong fix. A refused connection
    and a rejected password need different ones."""
    fresh("postgresql://nobody:nothing@127.0.0.1:59999/absent")
    status = store_mod.status()

    assert status["configured"] == "postgres"
    assert status["durable"] is False
    assert status["using"].startswith("memory")
    assert status["why_not"], "configured, not working, and nothing explains it"


def test_no_database_configured_is_not_reported_as_a_fault(fresh):
    """Memory is a supported setup. Flagging it as broken trains everybody to
    ignore the one line that matters when something really is."""
    built = fresh(None)
    assert built.durable is False
    assert store_mod.status() == {"configured": "memory", "durable": False,
                                  "using": "memory (lost on restart)", "why_not": ""}


# --------------------------------------------------------------------------
# the durable half
# --------------------------------------------------------------------------


@needs_db
def test_postgres_is_used_when_it_is_reachable(fresh):
    assert fresh(DATABASE_URL).durable is True
    assert store_mod.status() == {"configured": "postgres", "durable": True,
                                  "using": "postgres", "why_not": ""}


@needs_db
def test_a_trip_outlives_the_process_that_made_it(fresh):
    """The whole point. Two stores, built separately, sharing nothing but the
    database -- which is what a restart is."""
    written = fresh(DATABASE_URL).save(
        "owner-restart", {"bookings": [], "preference": "cheapest"}, "Tuesday's trip")

    read = fresh(DATABASE_URL).get(written.id)
    assert read is not None, "gone after a restart, which is the bug this exists for"
    assert (read.label, read.payload) == (written.label, written.payload)
    assert read.created == written.created, "a restart must not re-date a trip"


@needs_db
def test_an_update_keeps_the_id_the_traveller_is_holding(fresh):
    """Re-saving under a new one leaves them with a link to the version before
    the thing they just did."""
    db = fresh(DATABASE_URL)
    first = db.save("owner-update", {"step": 1}, "T")
    db.update(first.id, {"step": 2})

    after = fresh(DATABASE_URL).get(first.id)
    assert after.id == first.id and after.created == first.created
    assert after.payload == {"step": 2}


@needs_db
def test_one_traveller_never_sees_another_s_trips(fresh):
    db = fresh(DATABASE_URL)
    mine = db.save("owner-a", {"bookings": []}, "mine")
    db.save("owner-b", {"bookings": []}, "theirs")

    assert all(t.owner == "owner-a" for t in db.list("owner-a"))
    assert mine.id in {t.id for t in db.list("owner-a")}
    # And the owner is in the WHERE clause, not checked in Python first.
    assert db.delete(mine.id, "owner-b") is False
    assert db.get(mine.id) is not None


@needs_db
def test_the_watch_asks_the_database_for_its_work(fresh):
    """A watch that loads every itinerary anybody ever pasted, once a minute,
    is a watch that gets switched off."""
    db = fresh(DATABASE_URL)
    quiet = db.save("owner-watch", {"bookings": []}, "nothing wrong")
    broken = db.save("owner-watch", {"disruption": {"booking_id": "x"}}, "broken")

    watching = {t.id for t in db.watching()}
    assert broken.id in watching and quiet.id not in watching
