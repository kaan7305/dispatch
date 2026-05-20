"""Dispatch menu-bar tray app — fully native, no browser needed.

Windows:
  • "Open Dispatch"  → native window → sender UI  (localhost:8000)
  • Incoming task    → native window → consent UI  (localhost:8001)
  • Friend request   → macOS notification

Startup order:
  1. Start local broker  (own thread + event loop, wait until listening)
  2. Onboarding wizard   (first launch only)
  3. Login to broker     (saves JWT token)
  4. Start daemon loop   (own thread + event loop)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import certifi
import rumps
import ssl
import websockets

from dispatch.daemon.local_app import LocalState
from dispatch.daemon.main import handle_broker, serve_local_ui
from dispatch.shared.schema import DispatchPayload
from dispatch.tray.config import Config
from dispatch.tray.window import open_native_window, schedule_window

BROKER_URL     = "http://127.0.0.1:8000"
SENDER_UI_URL  = "http://127.0.0.1:8000"
CONSENT_UI_URL = "http://127.0.0.1:8001"

ICON_CONNECTED  = "⬡ Dispatch"
ICON_CONNECTING = "◌ Dispatch"


class DispatchTrayApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Dispatch", title=ICON_CONNECTING, quit_button=None)

        self.config = Config.load()
        self.config.broker_url = BROKER_URL

        self._broker_loop: asyncio.AbstractEventLoop | None = None
        self._daemon_loop:  asyncio.AbstractEventLoop | None = None
        self._broker_ready = threading.Event()

        self._status_item = rumps.MenuItem("Starting…")
        self._status_item.set_callback(None)

        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("Open Dispatch",     callback=self.open_sender),
            rumps.MenuItem("Incoming tasks",    callback=self.open_consent),
            None,
            rumps.MenuItem("Account…",          callback=self.show_account),
            rumps.MenuItem("Quit",              callback=self.quit_app),
        ]

        rumps.Timer(self._on_startup, 0.3).start()

    # ------------------------------------------------------------------
    # Startup sequence
    # ------------------------------------------------------------------

    def _on_startup(self, timer: rumps.Timer) -> None:
        timer.stop()
        self._set_status(ICON_CONNECTING, "Starting…")
        self._start_broker()
        import time as _time
        self._startup_deadline = _time.time() + 30  # 30-second wall-clock timeout
        # Persistent repeating timer on the main thread — avoids creating new timers in a
        # loop, which makes NSTimer fire immediately instead of after the interval.
        rumps.Timer(self._check_broker_ready, 0.2).start()

    def _check_broker_ready(self, timer: rumps.Timer) -> None:
        import time as _time
        if self._broker_ready.is_set():
            timer.stop()
            self._after_broker_ready()
        elif _time.time() > self._startup_deadline:
            timer.stop()
            log_path = Path("/tmp/dispatch_broker.log")
            detail = log_path.read_text() if log_path.exists() else "No log — unknown failure."
            rumps.alert(
                title="Dispatch — Broker failed to start",
                message=detail[:600] or "Unknown error. Check /tmp/dispatch_broker.log",
            )

    def _after_broker_ready(self) -> None:
        if not self.config.is_complete():
            self._run_onboarding()
        else:
            self._start_daemon()

    # ------------------------------------------------------------------
    # Local broker
    # ------------------------------------------------------------------

    def _start_broker(self) -> None:
        if not os.environ.get("DISPATCH_JWT_SECRET"):
            os.environ["DISPATCH_JWT_SECRET"] = hashlib.sha256(b"dispatch-local-secret").hexdigest()

        import signal as _signal, subprocess, time as _time
        try:
            pids = subprocess.run(
                ["lsof", "-ti:8000"], capture_output=True, text=True
            ).stdout.split()
            for pid in pids:
                os.kill(int(pid), _signal.SIGKILL)
            if pids:
                _time.sleep(0.5)
        except Exception:
            pass

        self._broker_ready.clear()
        self._broker_loop = asyncio.new_event_loop()

        ready_event = self._broker_ready

        def _run() -> None:
            asyncio.set_event_loop(self._broker_loop)
            self._broker_loop.run_until_complete(_broker_main(ready_event))

        threading.Thread(target=_run, daemon=True, name="dispatch-broker").start()

    # ------------------------------------------------------------------
    # One-time onboarding
    # ------------------------------------------------------------------

    def _run_onboarding(self) -> None:
        for label, attr, placeholder in [
            ("Choose a username",            "username",           "alice"),
            ("Paste your Anthropic API key", "anthropic_api_key",  "sk-ant-…"),
        ]:
            w = rumps.Window(
                message=label,
                title="Welcome to Dispatch",
                default_text=getattr(self.config, attr) or placeholder,
                ok="Next →",
                cancel="Quit",
                dimensions=(340, 24),
            )
            r = w.run()
            if not r.clicked:
                rumps.quit_application()
                return
            setattr(self.config, attr, r.text.strip())

        self._do_login()

    def _do_login(self) -> None:
        import httpx
        try:
            with httpx.Client() as client:
                resp = client.post(
                    f"{self.config.broker_url.rstrip('/')}/auth/login",
                    json={"username": self.config.username},
                    timeout=10,
                )
                resp.raise_for_status()
                self.config.token = resp.json()["token"]
                self.config.save()
        except Exception as exc:
            rumps.alert(title="Could not connect", message=str(exc))
            self._run_onboarding()
            return

        self._start_daemon()

    # ------------------------------------------------------------------
    # Daemon
    # ------------------------------------------------------------------

    def _start_daemon(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = self.config.anthropic_api_key
        self._daemon_loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(self._daemon_loop)
            try:
                self._daemon_loop.run_until_complete(self._daemon_main())
            except Exception:
                import traceback
                Path("/tmp/dispatch_daemon.log").write_text(traceback.format_exc())

        threading.Thread(target=_run, daemon=True, name="dispatch-daemon").start()

    async def _daemon_main(self) -> None:
        state = LocalState()
        state.user_id = self.config.username

        _orig = state.add_dispatch
        def _on_new_dispatch(payload: DispatchPayload) -> asyncio.Future:
            rumps.notification(
                title="New task from " + payload.sender_id,
                subtitle=payload.task[:80],
                message="Approve or deny in the Dispatch window.",
            )
            schedule_window(CONSENT_UI_URL, "Dispatch — Incoming Task")
            return _orig(payload)
        state.add_dispatch = _on_new_dispatch  # type: ignore[method-assign]

        workspace = Path(self.config.workspace).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        await serve_local_ui(state, self.config.ui_port)
        self._set_status(ICON_CONNECTING, "Connecting…")

        ws_url = self._ws_url("/agent/connect")
        backoff = 2

        def on_friend_request(from_user: str) -> None:
            rumps.notification(
                title="Friend request",
                subtitle=f"{from_user} wants to connect",
                message="Open Dispatch to accept or decline.",
            )
            schedule_window(SENDER_UI_URL, "Dispatch")

        while True:
            try:
                async with websockets.connect(ws_url, max_size=None) as ws:
                    self._set_status(ICON_CONNECTED, f"Online as {self.config.username}")
                    backoff = 2
                    await handle_broker(ws, state, workspace, on_friend_request=on_friend_request)
            except websockets.exceptions.InvalidStatus:
                self._set_status(ICON_CONNECTING, "Auth error — check Account")
                break
            except Exception:
                self._set_status(ICON_CONNECTING, f"Reconnecting in {backoff}s…")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _ws_url(self, path: str) -> str:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(self.config.broker_url)
        scheme = "wss" if p.scheme == "https" else "ws"
        return urlunparse((scheme, p.netloc, path, "", f"token={self.config.token}", ""))

    def _set_status(self, icon: str, detail: str) -> None:
        self.title = icon
        self._status_item.title = detail

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    @rumps.clicked("Open Dispatch")
    def open_sender(self, _: rumps.MenuItem) -> None:
        url = f"{SENDER_UI_URL}?token={self.config.token}&user_id={self.config.username}"
        open_native_window(url, "Dispatch")

    @rumps.clicked("Incoming tasks")
    def open_consent(self, _: rumps.MenuItem) -> None:
        open_native_window(CONSENT_UI_URL, "Dispatch — Incoming Tasks")

    def show_account(self, _: rumps.MenuItem) -> None:
        rumps.alert(title="Account", message=f"Signed in as: {self.config.username}", ok="OK")

    def quit_app(self, _: rumps.MenuItem) -> None:
        for loop in (self._daemon_loop, self._broker_loop):
            if loop and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        rumps.quit_application()


async def _broker_main(ready: threading.Event) -> None:
    log_path = Path("/tmp/dispatch_broker.log")
    log_path.unlink(missing_ok=True)
    try:
        from dispatch.broker.app import app as broker_app
        import uvicorn
        cfg = uvicorn.Config(broker_app, host="127.0.0.1", port=8000, log_level="warning")
        server = uvicorn.Server(cfg)

        serve_task = asyncio.create_task(server.serve())

        for _ in range(200):
            await asyncio.sleep(0.1)
            if server.started:
                ready.set()
                break
            if serve_task.done():
                break

        if not ready.is_set():
            exc = serve_task.exception() if serve_task.done() and not serve_task.cancelled() else None
            log_path.write_text(str(exc) if exc else "Broker did not become ready within 20 s")

        await serve_task
    except BaseException:
        import traceback
        log_path.write_text(traceback.format_exc())


def main() -> None:
    DispatchTrayApp().run()
