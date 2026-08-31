"""Where trips live between requests.

A free instance has an ephemeral filesystem: every restart and every redeploy
wipes the disk. A demo does not notice. A product where somebody pasted their
itinerary on Tuesday and finds it gone on Wednesday is not a product, so this is
the difference between the two.

Two implementations behind one interface, chosen by whether DATABASE_URL is set,
the same shape as `ports/`. Memory is the default so the tests, the CLI and a
laptop need no database; Postgres is what runs anywhere real.

WHAT IS DELIBERATELY NOT HERE. No caching layer, no ORM, no migration framework.
One table, created on first use, holding the same JSON the extractor produces —
so a stored trip and a freshly parsed one cannot drift apart, and the round-trip
is one test rather than a mapping layer nobody maintains.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id         TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    created    TIMESTAMPTZ NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    payload    JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_owner_idx ON trips (owner, created DESC);
CREATE TABLE IF NOT EXISTS profiles (
    owner      TEXT PRIMARY KEY,
    updated    TIMESTAMPTZ NOT NULL,
    payload    JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id         BIGSERIAL PRIMARY KEY,
    trip       TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL,
    payload    JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_trip_idx ON audit (trip, id);
CREATE TABLE IF NOT EXISTS people (
    id         TEXT PRIMARY KEY,
    token      TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created    TIMESTAMPTZ NOT NULL
);
"""


@dataclass(frozen=True)
class Saved:
    id: str
    owner: str
    created: datetime
    label: str
    payload: dict


def new_id() -> str:
    """Unguessable, because it is the only thing protecting one traveller's
    itinerary from anyone who tries the next number up."""
    return secrets.token_urlsafe(12)


class MemoryStore:
    """Default. Survives requests, not restarts, and says so."""

    durable = False

    def __init__(self) -> None:
        self._trips: dict[str, Saved] = {}
        self._profiles: dict[str, dict] = {}
        self._people: dict[str, dict] = {}       # token -> {id, name}
        self._audit: dict[str, list[dict]] = {}  # trip -> events, append-only
        self._lock = threading.Lock()

    def save(self, owner: str, payload: dict, label: str = "") -> Saved:
        record = Saved(new_id(), owner, datetime.now(timezone.utc), label, payload)
        with self._lock:
            self._trips[record.id] = record
        return record

    def get(self, trip_id: str) -> Saved | None:
        return self._trips.get(trip_id)

    def update(self, trip_id: str, payload: dict) -> Saved | None:
        """Replace a stored trip's payload, keeping its id and creation time.

        The id is what a traveller has in their hand. Re-saving under a new one
        would leave them holding a link to the version before the thing they
        just did.
        """
        with self._lock:
            record = self._trips.get(trip_id)
            if record is None:
                return None
            updated = Saved(record.id, record.owner, record.created,
                            record.label, payload)
            self._trips[trip_id] = updated
            return updated

    def list(self, owner: str, limit: int = 20) -> list[Saved]:
        """Trips this person owns, and trips somebody shared with them.

        One list, because the page shows one list: a traveller and their
        assistant looking at the same screen should see the same trip in it.
        What each may DO with it is the role's business, not this query's.
        """
        rows = [t for t in self._trips.values()
                if t.owner == owner or _member(t.payload, owner)]
        return sorted(rows, key=lambda t: t.created, reverse=True)[:limit]

    def live(self, limit: int = 50) -> list[Saved]:
        """Every trip, whoever owns it. For the watch, which serves everybody
        and has no person to ask on behalf of."""
        return sorted(self._trips.values(), key=lambda t: t.created,
                      reverse=True)[:limit]

    def watching(self, limit: int = 50) -> list[Saved]:
        """Trips with a live disruption -- the only ones a ticker has work for.

        Asked of the store rather than filtered in the caller so the database
        does it in the WHERE clause. A watch that loads every itinerary anybody
        ever pasted, once a minute, is a watch that gets switched off.
        """
        rows = [t for t in self._trips.values() if t.payload.get("disruption")]
        return sorted(rows, key=lambda t: t.created, reverse=True)[:limit]

    def delete(self, trip_id: str, owner: str) -> bool:
        with self._lock:
            record = self._trips.get(trip_id)
            if record is None or record.owner != owner:
                return False
            del self._trips[trip_id]
            return True

    # -- the traveller, as opposed to their trips ------------------------
    def profile(self, owner: str) -> dict:
        """The owner's profile, or an empty dict -- which is a real answer:
        nobody has said anything yet, and nothing is applied."""
        return dict(self._profiles.get(owner, {}))

    def save_profile(self, owner: str, payload: dict) -> dict:
        with self._lock:
            self._profiles[owner] = dict(payload)
        return dict(payload)

    def wipe(self, trips: bool = True, people: bool = False,
             profiles: bool = False) -> dict:
        """Delete whole categories, and say exactly how much of each went.

        For clearing test data between rounds -- old trips priced in a
        currency the app no longer speaks, story runs, tokens handed to
        testers. Counts come back because "wiped" without a number is a claim
        nobody can check against what they expected to lose.

        The audit trail goes with the trips: append-only means no API deletes
        an entry, and wiping test trips while keeping their decision log
        would leave a trail about itineraries that no longer exist.
        """
        with self._lock:
            gone = {"trips": len(self._trips) if trips else 0,
                    "people": len(self._people) if people else 0,
                    "profiles": len(self._profiles) if profiles else 0,
                    "audit": sum(len(v) for v in self._audit.values()) if trips else 0}
            if trips:
                self._trips.clear()
                self._audit.clear()
            if people:
                self._people.clear()
            if profiles:
                self._profiles.clear()
        return gone

    # -- the paper trail: append and read, nothing else -------------------
    def audit(self, trip_id: str, payload: dict) -> None:
        with self._lock:
            self._audit.setdefault(trip_id, []).append(dict(payload))

    def trail(self, trip_id: str, limit: int = 200) -> list[dict]:
        return [dict(e) for e in self._audit.get(trip_id, [])[-limit:]]

    # -- people: a name and the token that is them ------------------------
    def add_person(self, person_id: str, token: str, name: str) -> None:
        with self._lock:
            self._people[token] = {"id": person_id, "name": name}

    def person(self, token: str) -> dict | None:
        """Who holds this token, or None. The token is the whole of the
        security, so an unknown one is nobody rather than a guess."""
        found = self._people.get(token)
        return dict(found) if found else None


class PostgresStore:
    """DATABASE_URL. One table, created on first connect."""

    durable = True

    def __init__(self, url: str) -> None:
        import psycopg

        self._url = url
        self._connect = psycopg.connect
        with self._conn() as conn:
            conn.execute(SCHEMA)

    def _conn(self):
        return self._connect(self._url, autocommit=True)

    def save(self, owner: str, payload: dict, label: str = "") -> Saved:
        record = Saved(new_id(), owner, datetime.now(timezone.utc), label, payload)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO trips (id, owner, created, label, payload)"
                " VALUES (%s, %s, %s, %s, %s)",
                (record.id, record.owner, record.created, record.label,
                 json.dumps(record.payload)))
        return record

    def get(self, trip_id: str) -> Saved | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, owner, created, label, payload FROM trips WHERE id = %s",
                (trip_id,)).fetchone()
        return Saved(*row) if row else None

    def update(self, trip_id: str, payload: dict) -> Saved | None:
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE trips SET payload = %s WHERE id = %s"
                " RETURNING id, owner, created, label, payload",
                (json.dumps(payload), trip_id)).fetchone()
        return Saved(*row) if row else None

    def watching(self, limit: int = 50) -> list[Saved]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, owner, created, label, payload FROM trips"
                " WHERE payload ? 'disruption' ORDER BY created DESC LIMIT %s",
                (limit,)).fetchall()
        return [Saved(*r) for r in rows]

    def list(self, owner: str, limit: int = 20) -> list[Saved]:
        with self._conn() as conn:
            # Owned, or named in the members list -- the JSONB containment
            # test does the membership check in the database rather than by
            # loading every trip and asking in Python.
            rows = conn.execute(
                "SELECT id, owner, created, label, payload FROM trips"
                " WHERE owner = %s OR payload->'members' @> %s::jsonb"
                " ORDER BY created DESC LIMIT %s",
                (owner, json.dumps([{"person": owner}]), limit)).fetchall()
        return [Saved(*r) for r in rows]

    def live(self, limit: int = 50) -> list[Saved]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, owner, created, label, payload FROM trips"
                " ORDER BY created DESC LIMIT %s", (limit,)).fetchall()
        return [Saved(*r) for r in rows]

    def delete(self, trip_id: str, owner: str) -> bool:
        with self._conn() as conn:
            # Owner in the WHERE clause, not checked in Python first: one
            # statement means there is no window between the check and the
            # delete, and no path that deletes somebody else's trip.
            result = conn.execute(
                "DELETE FROM trips WHERE id = %s AND owner = %s", (trip_id, owner))
            return result.rowcount > 0

    def profile(self, owner: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM profiles WHERE owner = %s", (owner,)).fetchone()
        return dict(row[0]) if row else {}

    def save_profile(self, owner: str, payload: dict) -> dict:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO profiles (owner, updated, payload) VALUES (%s, %s, %s)"
                " ON CONFLICT (owner) DO UPDATE SET payload = EXCLUDED.payload,"
                " updated = EXCLUDED.updated",
                (owner, datetime.now(timezone.utc), json.dumps(payload)))
        return dict(payload)

    def wipe(self, trips: bool = True, people: bool = False,
             profiles: bool = False) -> dict:
        gone = {"trips": 0, "people": 0, "profiles": 0, "audit": 0}
        with self._conn() as conn:
            if trips:
                gone["trips"] = conn.execute("DELETE FROM trips").rowcount
                gone["audit"] = conn.execute("DELETE FROM audit").rowcount
            if people:
                gone["people"] = conn.execute("DELETE FROM people").rowcount
            if profiles:
                gone["profiles"] = conn.execute("DELETE FROM profiles").rowcount
        return gone

    def audit(self, trip_id: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit (trip, at, payload) VALUES (%s, %s, %s)",
                (trip_id, payload["at"], json.dumps(payload)))

    def trail(self, trip_id: str, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM audit WHERE trip = %s"
                " ORDER BY id DESC LIMIT %s", (trip_id, limit)).fetchall()
        return [dict(r[0]) for r in reversed(rows)]

    def add_person(self, person_id: str, token: str, name: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO people (id, token, name, created) VALUES (%s, %s, %s, %s)",
                (person_id, token, name, datetime.now(timezone.utc)))

    def person(self, token: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, name FROM people WHERE token = %s", (token,)).fetchone()
        return {"id": row[0], "name": row[1]} if row else None


def _member(payload: dict, person_id: str) -> bool:
    return any(m.get("person") == person_id for m in (payload.get("members") or []))


_store = None

#: Why the configured database is not being used, when one was configured and
#: could not be reached. Empty when there is nothing to explain -- either
#: Postgres is running or nobody asked for it. Read by /health, which is the
#: whole point: the difference between "memory because that is the setup" and
#: "memory because the database is down" is invisible from the outside and is
#: the only one that matters to somebody whose trip vanished.
unavailable = ""


def store():
    """The one store this process uses. Postgres if configured, memory if not.

    A DATABASE_URL that cannot be reached falls back to memory rather than
    taking the process with it. It used to raise, and `store()` is on the path
    of every request that touches a trip -- so a database that was merely
    asleep, which is the normal state of a free instance, turned the whole app
    into a 500 rather than into a slightly less durable one. Losing restarts is
    bad; losing the app is worse, and the first is recoverable by looking at
    /health while the second looks like the software being broken.

    Loud, though. The fallback is recorded and reported, because a product that
    quietly stops persisting is how somebody pastes an itinerary on Tuesday and
    finds it gone on Wednesday with nothing anywhere admitting why.
    """
    global _store, unavailable
    if _store is None:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            _store = MemoryStore()
        else:
            try:
                # Render hands out postgres:// ; psycopg wants postgresql://
                _store = PostgresStore(url.replace("postgres://", "postgresql://", 1))
                unavailable = ""
            except Exception as exc:                       # noqa: BLE001
                # The driver's own words. "could not connect" and "password
                # authentication failed" need different fixes, and a single
                # "storage unavailable" sends you to the wrong one.
                unavailable = f"{type(exc).__name__}: {exc}".strip()[:300]
                _store = MemoryStore()
    return _store


def status() -> dict:
    """What storage is actually doing, in the shape /health reports it."""
    live = store()
    configured = "postgres" if os.environ.get("DATABASE_URL", "").strip() else "memory"
    return {
        "configured": configured,
        "durable": live.durable,
        "using": "postgres" if live.durable else "memory (lost on restart)",
        "why_not": unavailable,
    }
