"""SQLite storage layer.

Concurrency model
-----------------
SQLite allows exactly one writer at a time. Every write runs inside
``BEGIN IMMEDIATE`` so the write lock is acquired *before* the next
revision number is computed. This guarantees that concurrent writers
are fully serialized: no duplicate revisions, no interleaved
(intermediate) states. Readers use WAL mode, so they never block the
single writer and always see the last committed state.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("LEDGER_DB_PATH", "/data/ledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    points         INTEGER NOT NULL,
    correction_of  TEXT REFERENCES events(event_id),
    payload        TEXT NOT NULL,          -- canonical request JSON, for idempotent replay
    revision       INTEGER NOT NULL UNIQUE,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_correction_of ON events(correction_of);
CREATE INDEX IF NOT EXISTS idx_events_revision      ON events(revision);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Serialized write transaction: lock first, then read/derive/write."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """Snapshot read: one consistent view of the database."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
