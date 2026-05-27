"""Dispatch menu-bar tray app.

Thin supervisor. Reads ~/.dispatch/config.json. If the broker URL + JWT
are missing it opens the broker's /install page in the user's browser
(where Clerk handles Google sign-in and the install command is shown);
otherwise it spawns the daemon in a background thread + event loop and
provides menu items to open the locally-served approval UI.
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import webbrowser
from pathlib import Path

import rumps

from dispatch.tray.config import Config
from dispatch.tray.window import open_native_window

DEFAULT_BROKER = os.environ.get("DISPATCH_BROKER", "").rstrip("/")

ICON_OK     = "⬡ Dispatch"
ICON_BUSY   = "◌ Dispatch"
ICON_ERROR  = "⚠ Dispatch"


class DispatchTrayApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Dispatch", title=ICON_BUSY, quit_button=None)

        self.config = Config.load()
        # CLI env var wins over saved config, so a tester can point at staging.
        if DEFAULT_BROKER:
            self.config.broker = DEFAULT_BROKER

        self._daemon_loop: asyncio.AbstractEventLoop | None = None
        self._main_q: queue.Queue = queue.Queue()
        rumps.Timer(self._drain_main_queue, 0.1).start()

        self._status_item = rumps.MenuItem("Starting…")
        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("Open Inbox",       callback=self.open_inbox),
            rumps.MenuItem("Open Web (broker)", callback=self.open_broker_ui),
            None,
            rumps.MenuItem("Sign in / install…", callback=self.open_install),
            rumps.MenuItem("Account…",          callback=self.show_account),
            rumps.MenuItem("Quit",              callback=self.quit_app),
        ]

        rumps.Timer(self._on_startup, 0.3).start()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, timer: rumps.Timer) -> None:
        timer.stop()
        if not self.config.is_complete():
            self._set_status(ICON_ERROR, "Not signed in — open install page")
            rumps.alert(
                title="Dispatch is not signed in",
                message=(
                    "Open the install page to sign in with Google. "
                    "After running the install command, restart the tray app."
                ),
                ok="Open install page",
            )
            self.open_install(None)
            return
        self._start_daemon()

    # ------------------------------------------------------------------
    # Daemon supervisor
    # ------------------------------------------------------------------

    def _start_daemon(self) -> None:
        if self.config.anthropic_api_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", self.config.anthropic_api_key)
        self._daemon_loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(self._daemon_loop)
            try:
                self._daemon_loop.run_until_complete(self._daemon_main())
            except Exception:
                import traceback
                Path("/tmp/dispatch_daemon.log").write_text(traceback.format_exc())
                self._set_status(ICON_ERROR, "Crashed — see /tmp/dispatch_daemon.log")

        threading.Thread(target=_run, daemon=True, name="dispatch-daemon").start()
        self._set_status(ICON_BUSY, "Connecting to broker…")

    async def _daemon_main(self) -> None:
        # Reach into the daemon's run_session by synthesizing the same
        # argparse Namespace it would normally parse from argv.
        from argparse import Namespace
        from dispatch.daemon.main import run_session, DEFAULT_WORKSPACE

        backoff = 2
        while True:
            args = Namespace(
                broker=self.config.broker,
                token=self.config.token,
                workspace=str(DEFAULT_WORKSPACE),
                anthropic_key=self.config.anthropic_api_key or None,
                local_port=self.config.local_port,
            )
            try:
                self._set_status(ICON_BUSY, "Connecting to broker…")
                rc = await run_session(args)
                if rc == 0:
                    # Clean disconnect — reconnect with short delay.
                    backoff = 2
                else:
                    self._set_status(
                        ICON_ERROR, f"Daemon exited with code {rc}. Retrying…"
                    )
            except Exception:
                self._set_status(ICON_ERROR, "Daemon error — retrying…")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _drain_main_queue(self, _: rumps.Timer) -> None:
        while True:
            try:
                fn = self._main_q.get_nowait()
                fn()
            except queue.Empty:
                break

    def _on_main(self, fn) -> None:
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self._main_q.put(fn)

    def _set_status(self, icon: str, detail: str) -> None:
        self._on_main(lambda: (
            setattr(self, "title", icon),
            setattr(self._status_item, "title", detail),
        ))

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def open_inbox(self, _: rumps.MenuItem | None) -> None:
        if not self.config.is_complete():
            self.open_install(None)
            return
        open_native_window(
            f"http://127.0.0.1:{self.config.local_port}",
            title="Dispatch — Inbox",
        )

    def open_broker_ui(self, _: rumps.MenuItem | None) -> None:
        target = self.config.broker or "https://web-production-700f0.up.railway.app"
        webbrowser.open(target)

    def open_install(self, _: rumps.MenuItem | None) -> None:
        target = (self.config.broker or "https://web-production-700f0.up.railway.app").rstrip("/")
        webbrowser.open(target)

    def show_account(self, _: rumps.MenuItem) -> None:
        from dispatch.daemon.main import verify_token_user
        user = verify_token_user(self.config.token) if self.config.token else "(not signed in)"
        rumps.alert(
            title="Account",
            message=f"Signed in as: {user}\nBroker: {self.config.broker or '(unset)'}",
            ok="OK",
        )

    def quit_app(self, _: rumps.MenuItem) -> None:
        loop = self._daemon_loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        rumps.quit_application()


def main() -> None:
    DispatchTrayApp().run()
