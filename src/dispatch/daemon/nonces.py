"""Durable replay guard for the recipient daemon.

A dispatch carries a one-time ``(sender_device, nonce)`` pair. The daemon
must accept each pair at most once — otherwise a captured dispatch could
be replayed and run the agent a second time.

The pair set used to live in memory (``DaemonState.seen_nonces``), which
reset on every daemon restart. That forced a short signature-freshness
window (5 min) as a backstop: even if the in-memory set was wiped, a
captured dispatch was only valid briefly. But that same short window made
genuine *offline / async* delivery impossible — a dispatch queued for an
hour was rejected as stale on delivery.

Persisting the seen pairs across restarts removes the need for the short
window: replays are caught by "have I seen this nonce?" rather than by a
clock, so the freshness window can be widened to a long retention horizon
and dispatches can wait for an offline recipient for days.

Storage cost is trivial — each row is the device id + a 22-char nonce +
a timestamp; at a handful of dispatches a day, 30 days of history is tens
of kilobytes. Lookups are an indexed primary-key probe. Pruning keeps the
table bounded to the retention window.

Invariant: ``retention_seconds`` MUST be >= the maximum lifetime a
dispatch can legitimately have (its expiry / the freshness window), or a
dispatch could outlive its own nonce record and become replayable.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class NonceStore:
    """SQLite-backed set of accepted ``(sender_device, nonce)`` pairs.

    Single-threaded use from the daemon's event loop: the verify step is
    synchronous and runs to completion before the next broker frame is
    processed, so check-then-record needs no extra locking. Each row also
    carries the time it was first seen, so the set can be pruned to the
    retention window.
    """

    def __init__(self, path: Path, retention_seconds: float) -> None:
        self.path = Path(path)
        self.retention_seconds = retention_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the daemon may touch this from tasks
        # scheduled on the same loop; access is still effectively serial.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_nonces (
                sender_device TEXT NOT NULL,
                nonce         TEXT NOT NULL,
                seen_at       REAL NOT NULL,
                PRIMARY KEY (sender_device, nonce)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_nonces_seen_at ON seen_nonces(seen_at)"
        )
        self._conn.commit()

    def seen(self, sender_device: str, nonce: str) -> bool:
        """True iff this pair was already recorded (i.e. a replay)."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_nonces WHERE sender_device = ? AND nonce = ? LIMIT 1",
            (sender_device, nonce),
        ).fetchone()
        return row is not None

    def record(self, sender_device: str, nonce: str, now: float) -> None:
        """Mark a pair as accepted. Idempotent: a duplicate is a no-op."""
        self._conn.execute(
            """
            INSERT INTO seen_nonces (sender_device, nonce, seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT (sender_device, nonce) DO NOTHING
            """,
            (sender_device, nonce, now),
        )
        self._conn.commit()

    def prune(self, now: float) -> int:
        """Drop pairs older than the retention window. Returns rows removed."""
        cur = self._conn.execute(
            "DELETE FROM seen_nonces WHERE seen_at < ?",
            (now - self.retention_seconds,),
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
