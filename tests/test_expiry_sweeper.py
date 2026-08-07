"""The broker's expiry sweeper (src/dispatch/broker/app.py) must be
event-driven: zero Postgres queries while idle, wake exactly when the
earliest tracked dispatch expiry lapses, and never die on a DB error.

No Postgres needed — STORE is monkeypatched with a fake exposing just the
two methods the sweeper calls, so these exercise the scheduling logic
(heap tracking, wake-on-note, indefinite idle sleep, exception survival)
in isolation. Async bodies use asyncio.run(), matching this repo's existing
convention (see test_broker_http.py, test_sms.py) rather than pytest-asyncio.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from dispatch.broker import app as broker_app


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeStore:
    """Stands in for STORE: the only two methods the sweeper calls."""

    def __init__(self, earliest=None):
        self.expire_overdue_calls = 0
        self._earliest = earliest
        self.fail_next = False

    async def expire_overdue(self):
        self.expire_overdue_calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated DB failure")
        return []  # no rows: tests only care about call volume/timing

    async def earliest_pending_expiry(self):
        return self._earliest


def _fresh_state(monkeypatch, **kwargs) -> FakeStore:
    broker_app._expiry_heap.clear()
    broker_app._expiry_wake.clear()
    fake = FakeStore(**kwargs)
    monkeypatch.setattr(broker_app, "STORE", fake)
    return fake


async def _run_and_cancel(task: asyncio.Task, seconds: float) -> None:
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_note_expiry_pushes_heap_and_sets_wake():
    broker_app._expiry_heap.clear()
    broker_app._expiry_wake.clear()
    try:
        broker_app._note_expiry(_now() + timedelta(seconds=5))
        assert len(broker_app._expiry_heap) == 1
        assert broker_app._expiry_wake.is_set()
    finally:
        broker_app._expiry_heap.clear()
        broker_app._expiry_wake.clear()


def test_idle_sweeper_issues_only_the_startup_seed_query(monkeypatch):
    """The whole point of the redesign: nothing pending -> the sweeper blocks
    on the wake Event forever, issuing no further queries."""
    fake = _fresh_state(monkeypatch)

    async def body():
        task = asyncio.create_task(broker_app._expiry_sweeper())
        await _run_and_cancel(task, 0.3)

    asyncio.run(body())
    assert fake.expire_overdue_calls == 1


def test_note_expiry_wakes_sweeper_before_its_deadline(monkeypatch):
    fake = _fresh_state(monkeypatch)

    async def body():
        task = asyncio.create_task(broker_app._expiry_sweeper())
        await asyncio.sleep(0.05)
        assert fake.expire_overdue_calls == 1  # startup seed only, so far

        broker_app._note_expiry(_now() + timedelta(seconds=0.1))
        await _run_and_cancel(task, 0.4)

    asyncio.run(body())
    # The noted deadline fired a real sweep — no fixed interval was waited out.
    assert fake.expire_overdue_calls == 2


def test_burst_of_earlier_expiries_tracks_the_earliest(monkeypatch):
    """A burst of writes where later ones are earlier than the current sleep
    target must wake the sweeper early and re-target — not sleep out a stale
    (later) deadline first."""
    fake = _fresh_state(monkeypatch)

    async def body():
        task = asyncio.create_task(broker_app._expiry_sweeper())
        await asyncio.sleep(0.02)

        broker_app._note_expiry(_now() + timedelta(seconds=10))
        broker_app._note_expiry(_now() + timedelta(seconds=5))
        broker_app._note_expiry(_now() + timedelta(seconds=0.1))

        await _run_and_cancel(task, 0.4)

    asyncio.run(body())
    # startup seed + the 0.1s deadline firing; the 5s/10s ones are still pending.
    assert fake.expire_overdue_calls == 2
    assert len(broker_app._expiry_heap) == 2


def test_sweeper_survives_a_db_exception(monkeypatch):
    """A DB error (e.g. mid-outage) must be logged and swallowed, never take
    the sweeper task down — it has to keep running for the life of the app."""
    fake = _fresh_state(monkeypatch)
    fake.fail_next = True  # the startup seed's expire_overdue() call raises

    async def body():
        task = asyncio.create_task(broker_app._expiry_sweeper())
        await asyncio.sleep(0.05)
        assert not task.done()

        broker_app._note_expiry(_now() + timedelta(seconds=0.1))
        await _run_and_cancel(task, 0.4)

    asyncio.run(body())
    # First call failed; the noted deadline still triggered a successful sweep.
    assert fake.expire_overdue_calls >= 2


def test_health_endpoint_does_not_query_the_database(monkeypatch):
    """Railway polls /health continuously; a DB round-trip there would
    re-wake Neon forever regardless of how the sweeper behaves."""

    class ExplodingPool:
        def acquire(self):
            raise AssertionError("/health must never touch the database")

    class FakePoolHolder:
        pool = ExplodingPool()

    monkeypatch.setattr(broker_app, "STORE", FakePoolHolder())
    result = asyncio.run(broker_app.health())
    assert result == {"status": "ok", "database": "up"}
