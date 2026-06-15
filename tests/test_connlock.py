"""Tests for the machine-level broker-connection lock.

The daemon's tray supervisor runs ``run_session`` in a ``while True:`` loop
inside one process, and each iteration acquires this lock anew. If an
iteration ever leaks the flock (fails to ``release()``), the *next* iteration
in the same process can never reacquire it — BSD ``flock`` treats a second
open file description in the same process as a conflicting holder — and the
daemon wedges forever on "another process owns the broker connection;
standing by…". These tests pin the lock's contract so that regression can't
come back silently.
"""
import os

import pytest

from dispatch.daemon.connlock import ConnectionLock

pytestmark = pytest.mark.skipif(
    not hasattr(__import__("dispatch.daemon.connlock", fromlist=["_HAVE_FCNTL"]), "_HAVE_FCNTL")
    or not __import__("dispatch.daemon.connlock", fromlist=["_HAVE_FCNTL"])._HAVE_FCNTL,
    reason="flock unavailable on this platform",
)


def test_acquire_is_idempotent(tmp_path):
    lock = ConnectionLock(tmp_path / "conn.lock")
    assert lock.acquire() is True
    assert lock.acquire() is True  # already held → still True, no second fd
    lock.release()


def test_second_holder_in_same_process_conflicts(tmp_path):
    """This is the deadlock mechanism: a *leaked* lock blocks reacquire even
    from the same process, because each ConnectionLock opens its own fd."""
    held = ConnectionLock(tmp_path / "conn.lock")
    assert held.acquire() is True

    standby = ConnectionLock(tmp_path / "conn.lock")
    assert standby.acquire() is False  # can't win while `held` keeps the flock

    held.release()  # the missing release in the bug — once present, failover works
    assert standby.acquire() is True
    standby.release()


def test_release_allows_reacquire_loop(tmp_path):
    """Models the supervisor loop: acquire → release → reacquire, repeatedly.
    Without the guaranteed release this is exactly what deadlocked."""
    path = tmp_path / "conn.lock"
    for _ in range(5):
        lock = ConnectionLock(path)
        assert lock.acquire() is True
        lock.release()


def test_owner_sidecar_roundtrip_and_cleared_on_release(tmp_path):
    lock = ConnectionLock(tmp_path / "conn.lock")
    assert lock.acquire() is True
    lock.write_owner(role="daemon", local_port=8001)
    meta = lock.read_owner()
    assert meta["pid"] == os.getpid()
    assert meta["role"] == "daemon"
    assert meta["local_port"] == 8001
    lock.release()
    assert lock.read_owner() == {}  # sidecar cleared when it still named us


def test_release_is_safe_when_not_held(tmp_path):
    lock = ConnectionLock(tmp_path / "conn.lock")
    lock.release()  # no-op, must not raise
    assert lock.held is False
