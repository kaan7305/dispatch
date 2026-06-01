"""Tests for the durable replay guard (daemon.nonces.NonceStore)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dispatch.daemon.nonces import NonceStore  # noqa: E402


def test_record_and_replay(tmp_path):
    store = NonceStore(tmp_path / "n.db", retention_seconds=1000)
    assert store.seen("devA", "nonce1") is False
    store.record("devA", "nonce1", now=100.0)
    # Same pair is now a replay.
    assert store.seen("devA", "nonce1") is True
    # A different nonce, or same nonce from another device, is still fresh.
    assert store.seen("devA", "nonce2") is False
    assert store.seen("devB", "nonce1") is False
    store.close()


def test_record_is_idempotent(tmp_path):
    store = NonceStore(tmp_path / "n.db", retention_seconds=1000)
    store.record("devA", "n", now=1.0)
    store.record("devA", "n", now=2.0)  # no error, no duplicate
    assert store.seen("devA", "n") is True
    store.close()


def test_survives_reopen(tmp_path):
    path = tmp_path / "n.db"
    s1 = NonceStore(path, retention_seconds=1000)
    s1.record("devA", "n", now=100.0)
    s1.close()
    # A fresh process / restart re-opens the same file — the pair persists.
    s2 = NonceStore(path, retention_seconds=1000)
    assert s2.seen("devA", "n") is True
    s2.close()


def test_prune_drops_old_pairs(tmp_path):
    store = NonceStore(tmp_path / "n.db", retention_seconds=100)
    store.record("devA", "old", now=0.0)
    store.record("devA", "new", now=1000.0)
    removed = store.prune(now=1050.0)  # cutoff = 950; "old" is older
    assert removed == 1
    assert store.seen("devA", "old") is False
    assert store.seen("devA", "new") is True
    store.close()
