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
        self._lock = threading.Lock()

    def save(self, owner: str, payload: dict, label: str = "") -> Saved:
        record = Saved(new_id(), owner, datetime.now(timezone.utc), label, payload)
        with self._lock:
            self._trips[record.id] = record
        return record

    def get(self, trip_id: str) -> Saved | None:
        return self._trips.get(trip_id)

    def list(self, owner: str, limit: int = 20) -> list[Saved]:
        rows = [t for t in self._trips.values() if t.owner == owner]
        return sorted(rows, key=lambda t: t.created, reverse=True)[:limit]

    def delete(self, trip_id: str, owner: str) -> bool:
        with self._lock:
            record = self._trips.get(trip_id)
            if record is None or record.owner != owner:
                return False
            del self._trips[trip_id]
            return True


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

    def list(self, owner: str, limit: int = 20) -> list[Saved]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, owner, created, label, payload FROM trips"
                " WHERE owner = %s ORDER BY created DESC LIMIT %s",
                (owner, limit)).fetchall()
        return [Saved(*r) for r in rows]

    def delete(self, trip_id: str, owner: str) -> bool:
        with self._conn() as conn:
            # Owner in the WHERE clause, not checked in Python first: one
            # statement means there is no window between the check and the
            # delete, and no path that deletes somebody else's trip.
            result = conn.execute(
                "DELETE FROM trips WHERE id = %s AND owner = %s", (trip_id, owner))
            return result.rowcount > 0


_store = None


def store():
    """The one store this process uses. Postgres if configured, memory if not."""
    global _store
    if _store is None:
        url = os.environ.get("DATABASE_URL", "").strip()
        if url:
            # Render hands out postgres:// ; psycopg wants postgresql://
            _store = PostgresStore(url.replace("postgres://", "postgresql://", 1))
        else:
            _store = MemoryStore()
    return _store
