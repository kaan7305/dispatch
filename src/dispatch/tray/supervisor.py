"""Platform-neutral daemon supervision.

The tray's real job is not the icon — it is being the thing that keeps a daemon
alive: run it in a background thread, restart it when credentials change or the
machine wakes, notice when ``dispatch update`` has installed newer code, and
report state to whatever can display it.

None of that is macOS-specific, but on macOS it was written directly against
``rumps.Timer`` (an NSTimer on the AppKit run loop) and interleaved with
AppKit calls, so a second platform could not reuse a line of it. This module is
that logic with the UI removed: it takes callbacks and owns its own scheduler,
so a pystray tray, a Cocoa tray, or a headless test can each drive it.

The macOS tray still runs its own copy. This is deliberate: the AppKit version
works and cannot be exercised from here, and a refactor that could only be
tested on one of the two platforms is a worse trade than the duplication.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from dispatch.tray.config import Config

logger = logging.getLogger("dispatch.tray.supervisor")

# Connection states the supervisor reports. The UI maps these to whatever it
# can show — a menu bar title on macOS, an icon plus tooltip on Windows.
STATE_OK = "ok"
STATE_BUSY = "busy"
STATE_ERROR = "error"

StatusFn = Callable[[str, str], None]
NotifyFn = Callable[..., None]


def read_installed_commit() -> str:
    """The commit ``dispatch update`` last installed on disk.

    The running process stays on the code it imported at startup until it
    re-execs, so comparing this marker against the value read at startup is how
    we detect "the daemon is running stale code".
    """
    try:
        return (
            (Path.home() / ".dispatch" / "installed_commit")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        return ""


class Scheduler:
    """A tiny timer wheel on one background thread.

    Replaces ``rumps.Timer``. The UI toolkit must not be assumed to have a
    run loop we can post to: pystray's does not expose one, and the supervisor
    needs delayed work (restart-after-teardown, the update poll) regardless of
    which tray is on top of it.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="dispatch-tray-scheduler", daemon=True
        )
        self._thread.start()

    def call_later(self, delay: float, fn: Callable[[], None]) -> None:
        with self._lock:
            heapq.heappush(self._heap, (time.monotonic() + delay, next(self._seq), fn))
        self._wake.set()

    def every(self, interval: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` repeatedly. Stops when the scheduler stops."""

        def tick() -> None:
            try:
                fn()
            finally:
                if not self._stop.is_set():
                    self.call_later(interval, tick)

        self.call_later(interval, tick)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                now = time.monotonic()
                if self._heap and self._heap[0][0] <= now:
                    _, _, fn = heapq.heappop(self._heap)
                else:
                    fn = None
                    timeout = (self._heap[0][0] - now) if self._heap else 1.0
            if fn is not None:
                try:
                    fn()
                except Exception:
                    # One bad callback must not take the scheduler — and with
                    # it the update poll and every pending restart — down.
                    logger.exception("scheduled tray callback failed")
                continue
            self._wake.wait(timeout=max(0.01, min(timeout, 1.0)))
            self._wake.clear()


class DaemonSupervisor:
    """Runs ``dispatch.daemon.main.run_session`` in a supervised thread."""

    def __init__(
        self,
        *,
        on_status: StatusFn,
        on_notify: NotifyFn,
        scheduler: Optional[Scheduler] = None,
    ) -> None:
        self.config = Config.load()
        self._on_status = on_status
        self._on_notify = on_notify
        self.scheduler = scheduler or Scheduler()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._last_wake_reconnect = 0.0

        self.started_commit = read_installed_commit()
        self.update_pending = False
        self.signed_out = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def local_port(self) -> int:
        return self.config.local_port

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the daemon thread, unless one is already alive."""
        if self.running:
            return
        self._stopping = False

        import os

        if self.config.anthropic_api_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", self.config.anthropic_api_key)

        loop = asyncio.new_event_loop()
        self._loop = loop
        log_path = Path.home() / ".dispatch" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def _run() -> None:
            asyncio.set_event_loop(loop)
            # errors="replace" and an explicit encoding: the daemon prints task
            # titles and file paths, and on Windows the default codec is the
            # ANSI codepage, where the first non-ASCII character raises inside
            # the logging path and takes the daemon thread with it.
            try:
                log_file = open(
                    log_path, "w", buffering=1, encoding="utf-8", errors="replace"
                )
            except OSError:
                log_file = None
            try:
                if log_file is not None:
                    log_file.write(
                        f"=== daemon start @ broker={self.config.broker} "
                        f"port={self.config.local_port} ===\n"
                    )
                    import sys as _sys

                    _sys.stdout = log_file
                    _sys.stderr = log_file
                loop.run_until_complete(self._main())
            except (asyncio.CancelledError, RuntimeError) as e:
                if log_file is not None:
                    log_file.write(f"=== daemon stopped: {type(e).__name__} ===\n")
            except Exception:
                import traceback

                if log_file is not None:
                    log_file.write("=== daemon crashed ===\n")
                    traceback.print_exc(file=log_file)
                self._on_status(STATE_ERROR, "Crashed — see ~/.dispatch/daemon.log")
            finally:
                if log_file is not None:
                    try:
                        log_file.flush()
                        log_file.close()
                    except Exception:
                        pass

        thread = threading.Thread(target=_run, daemon=True, name="dispatch-daemon")
        self._thread = thread
        thread.start()
        self._on_status(STATE_BUSY, "Connecting to broker…")

    def restart(self) -> None:
        """Cancel the running session and start again on current config.

        Safe from any thread. Used when credentials change, on sign-out, and
        after the machine wakes.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(
                lambda: [t.cancel() for t in asyncio.all_tasks(loop)]
            )
        self._loop = None
        self.scheduler.call_later(1.5, self._delayed_start)

    def _delayed_start(self) -> None:
        # Wait for the old thread to release the port before binding it again.
        thread = self._thread
        if thread is not None and thread.is_alive():
            self.scheduler.call_later(0.5, self._delayed_start)
            return
        # Re-read from disk: a sign-out wrote to config.json but our copy is
        # stale, and restarting with the deleted token just fails again.
        self.config = Config.load()
        if self.config.is_complete():
            self.signed_out = False
            self.start()
        else:
            self.signed_out = True
            self._on_status(STATE_ERROR, "Signed out — sign in at the broker")

    def stop(self) -> None:
        self._stopping = True
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        self.scheduler.stop()

    def on_system_wake(self) -> None:
        """Force an immediate reconnect after the machine wakes.

        The broker WebSocket is usually a zombie at this point: the socket died
        while the machine slept but nothing notices until a keepalive times
        out. Debounced, because a wake delivers a burst of notifications.
        """
        now = time.monotonic()
        if now - self._last_wake_reconnect < 5.0:
            return
        self._last_wake_reconnect = now
        if not self.config.is_complete() or self._loop is None:
            return
        self._on_status(STATE_BUSY, "Woke — reconnecting…")
        self.restart()

    # -- the session loop --------------------------------------------------

    async def _main(self) -> None:
        from argparse import Namespace

        from dispatch.daemon.main import DEFAULT_WORKSPACE, run_session

        def on_status(state: str) -> None:
            if state == "enrolling":
                self._on_status(STATE_BUSY, "Enrolling device…")
            elif state == "connecting":
                self._on_status(STATE_BUSY, "Connecting to broker…")
            elif state == "connected":
                self._on_status(STATE_OK, f"Online — {self.account_label()}")
            elif state == "disconnected":
                self._on_status(STATE_BUSY, "Reconnecting…")

        def on_notification(
            title: str, subtitle: str, message: str, dispatch_id: str | None = None
        ) -> None:
            self._on_notify(title, subtitle, message, dispatch_id=dispatch_id)

        def on_signout() -> None:
            self.restart()

        def on_recheck() -> None:
            self.check_for_update()

        backoff = 2
        while not self._stopping:
            args = Namespace(
                # No pinned broker: run_session multi-homes across every entry
                # in ~/.dispatch/config.json, legacy flat keys included.
                broker=None,
                token=None,
                workspace=str(DEFAULT_WORKSPACE),
                anthropic_key=self.config.anthropic_api_key or None,
                local_port=self.config.local_port,
            )
            try:
                self._on_status(STATE_BUSY, "Starting…")
                rc = await run_session(
                    args,
                    on_status=on_status,
                    on_notification=on_notification,
                    on_signout=on_signout,
                    on_recheck=on_recheck,
                )
                if rc == 7:
                    # The broker says the user signed out. Stop reconnecting.
                    self.config = Config.load()
                    self.signed_out = True
                    self._on_status(STATE_ERROR, "Signed out at broker")
                    self._on_notify(
                        "Dispatch — signed out",
                        "You signed out at the broker.",
                        "Open Broker to sign in again.",
                    )
                    return
                if rc == 0:
                    backoff = 2
                    self._on_status(STATE_BUSY, "Reconnecting…")
                else:
                    self._on_status(STATE_ERROR, f"Daemon exited with code {rc}. Retrying…")
            except asyncio.CancelledError:
                raise  # restart() or stop() is unwinding us
            except Exception:
                # Written next to the other logs. The macOS tray wrote this to
                # /tmp, which resolves to C:\tmp on Windows — a directory that
                # does not exist on a stock install, so the traceback for the
                # crash you are trying to diagnose was itself lost to an
                # exception inside the exception handler.
                import traceback

                try:
                    (Path.home() / ".dispatch" / "daemon-retry.log").write_text(
                        traceback.format_exc(), encoding="utf-8"
                    )
                except OSError:
                    pass
                self._on_status(STATE_ERROR, "Daemon error — retrying…")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # -- account + updates -------------------------------------------------

    def account_label(self) -> str:
        try:
            from dispatch.daemon.main import verify_token_user

            if self.config.token:
                return verify_token_user(self.config.token) or "Dispatch"
        except Exception:
            pass
        return "Dispatch"

    def check_for_update(self) -> bool:
        """True the first time newer code is detected on disk."""
        current = read_installed_commit()
        if not current:
            return False
        if not self.started_commit:
            # Started before any marker existed — adopt the first value seen as
            # the baseline instead of crying "outdated" spuriously.
            self.started_commit = current
            return False
        if current != self.started_commit and not self.update_pending:
            self.update_pending = True
            return True
        return False
