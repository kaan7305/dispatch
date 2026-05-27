"""Dispatch menu-bar tray app.

Thin supervisor. Reads ~/.dispatch/config.json. If the broker URL + JWT
are missing it opens the broker's /install page in the user's browser
(where Clerk handles Google sign-in and the install command is shown);
otherwise it spawns the daemon in a background thread + event loop and
provides menu items to open the locally-served desktop UI.

Also handles the dispatch:// URL scheme: when the broker install page
launches `dispatch://configure?broker=...&token=...&user_id=...&api_key=...`
after Clerk sign-in, macOS hands the URL to this process via Cocoa.
We parse it, write config, and (re)start the daemon — turnkey onboarding
with no terminal.
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import objc
import rumps
from AppKit import NSAppleEventManager, NSObject
from Foundation import NSURL  # type: ignore  # noqa: F401

from dispatch.tray import autostart
from dispatch.tray.config import Config
from dispatch.tray.window import open_native_window

DEFAULT_BROKER = os.environ.get("DISPATCH_BROKER", "").rstrip("/")

ICON_OK     = "⬡ Dispatch"
ICON_BUSY   = "◌ Dispatch"
ICON_ERROR  = "⚠ Dispatch"


class _URLHandler(NSObject):
    """Cocoa delegate that receives dispatch:// URLs via Apple Events.

    Stored on the DispatchTrayApp instance so that handleURL_ can forward
    the parsed config back into the rumps event loop.
    """
    def initWithApp_(self, tray_app):  # noqa: N802 — ObjC selector
        self = objc.super(_URLHandler, self).init()
        if self is None:
            return None
        self._tray = tray_app  # type: ignore[attr-defined]
        return self

    def handleEvent_withReplyEvent_(self, event, _reply):  # noqa: N802
        try:
            descriptor = event.paramDescriptorForKeyword_(0x2D2D2D2D)  # '----'
            url_string = descriptor.stringValue() if descriptor else ""
        except Exception:
            url_string = ""
        if url_string and url_string.startswith("dispatch://"):
            self._tray._on_main(lambda u=url_string: self._tray._handle_dispatch_url(u))


class DispatchTrayApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Dispatch", title=ICON_BUSY, quit_button=None)

        self.config = Config.load()
        # CLI env var wins over saved config, so a tester can point at staging.
        if DEFAULT_BROKER:
            self.config.broker = DEFAULT_BROKER

        # Register the dispatch:// URL handler before rumps takes over the
        # run loop. macOS routes Apple-Event URL opens here.
        self._url_handler = _URLHandler.alloc().initWithApp_(self)
        NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
            self._url_handler,
            b"handleEvent:withReplyEvent:",
            0x4755524C,  # 'GURL'
            0x4755524C,  # 'GURL'
        )

        self._daemon_loop: asyncio.AbstractEventLoop | None = None
        self._main_q: queue.Queue = queue.Queue()
        rumps.Timer(self._drain_main_queue, 0.1).start()

        self._status_item = rumps.MenuItem("Starting…")
        self._autostart_item = rumps.MenuItem(
            "Start at login", callback=self.toggle_autostart,
        )
        self._autostart_item.state = 1 if autostart.is_enabled() else 0

        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("Open Inbox",   callback=self.open_inbox),
            rumps.MenuItem("Open Broker",  callback=self.open_broker_ui),
            None,
            self._autostart_item,
            rumps.MenuItem("Account…",     callback=self.show_account),
            rumps.MenuItem("Quit",         callback=self.quit_app),
        ]

        rumps.Timer(self._on_startup, 0.3).start()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, timer: rumps.Timer) -> None:
        timer.stop()
        if not self.config.is_complete():
            broker = self.config.broker or "https://web-production-700f0.up.railway.app"
            self._set_status(ICON_ERROR, "Not signed in — run installer")
            rumps.alert(
                title="Dispatch is not signed in",
                message=(
                    f"Visit {broker} in your browser, sign in with Google, "
                    "and run the install command shown there. Then restart "
                    "the tray app."
                ),
                ok="OK",
            )
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

        def on_status(state: str) -> None:
            if state == "enrolling":
                self._set_status(ICON_BUSY, "Enrolling device…")
            elif state == "connecting":
                self._set_status(ICON_BUSY, "Connecting to broker…")
            elif state == "connected":
                user = self.config.token and (
                    __import__("dispatch.daemon.main", fromlist=["verify_token_user"])
                    .verify_token_user(self.config.token)
                ) or "Dispatch"
                self._set_status(ICON_OK, f"Online — {user}")
            elif state == "disconnected":
                self._set_status(ICON_BUSY, "Reconnecting…")

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
                self._set_status(ICON_BUSY, "Starting…")
                rc = await run_session(args, on_status=on_status)
                if rc == 0:
                    backoff = 2
                    self._set_status(ICON_BUSY, "Reconnecting…")
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
            rumps.alert(
                title="Not signed in",
                message="Run the install command first, then restart the tray app.",
                ok="OK",
            )
            return
        from dispatch.daemon.local_app import read_local_token
        token = read_local_token()
        suffix = f"#t={token}" if token else ""
        open_native_window(
            f"http://127.0.0.1:{self.config.local_port}/{suffix}",
            title="Dispatch — Inbox",
        )

    def open_broker_ui(self, _: rumps.MenuItem | None) -> None:
        target = self.config.broker or "https://web-production-700f0.up.railway.app"
        webbrowser.open(target)

    # ------------------------------------------------------------------
    # dispatch:// URL handler
    # ------------------------------------------------------------------

    def _handle_dispatch_url(self, url: str) -> None:
        """Parse a dispatch://configure?broker=...&token=...&user_id=...
        URL, persist the values, and start the daemon if it wasn't already
        running. Called on the main thread."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "dispatch" or parsed.netloc != "configure":
            return
        params = urllib.parse.parse_qs(parsed.query)

        broker  = (params.get("broker") or [""])[0].rstrip("/")
        token   = (params.get("token") or [""])[0]
        api_key = (params.get("api_key") or [""])[0]
        if not (broker and token):
            rumps.alert(
                title="Dispatch — bad install link",
                message="The link was missing broker URL or token. Try again from the install page.",
                ok="OK",
            )
            return

        was_complete = self.config.is_complete()
        self.config.broker = broker
        self.config.token = token
        if api_key:
            self.config.anthropic_api_key = api_key
        self.config.save()

        if not was_complete:
            self._start_daemon()
            rumps.notification(
                title="Dispatch is configured",
                subtitle=f"Signed in. Daemon starting…",
                message="Click the menu bar icon → Open Inbox.",
            )
        else:
            rumps.notification(
                title="Dispatch credentials updated",
                subtitle="Restart the app to pick up the new token.",
                message="",
            )

    def toggle_autostart(self, item: rumps.MenuItem) -> None:
        if autostart.is_enabled():
            autostart.disable()
            item.state = 0
        else:
            autostart.enable()
            item.state = 1

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
