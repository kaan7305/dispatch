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

BROKER_URL = (
    os.environ.get("DISPATCH_BROKER", "https://web-production-700f0.up.railway.app")
    .rstrip("/")
)

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
        if BROKER_URL:
            self.config.broker = BROKER_URL

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
        self._daemon_thread: threading.Thread | None = None
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
            rumps.MenuItem("View Log",     callback=self.view_log),
            rumps.MenuItem("Quit",         callback=self.quit_app),
        ]

        rumps.Timer(self._on_startup, 0.3).start()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, timer: rumps.Timer) -> None:
        timer.stop()
        if not self.config.is_complete():
            broker = self.config.broker or BROKER_URL
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
        # _handle_dispatch_url may have already started the daemon if the
        # deep link arrived within the 0.3 s startup window — don't double-start.
        if self._daemon_thread is None or not self._daemon_thread.is_alive():
            self._start_daemon()

    # ------------------------------------------------------------------
    # Daemon supervisor
    # ------------------------------------------------------------------

    def _start_daemon(self) -> None:
        # Guard: never start a second daemon while one is still alive.
        if self._daemon_thread is not None and self._daemon_thread.is_alive():
            return

        if self.config.anthropic_api_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", self.config.anthropic_api_key)

        # Capture loop locally so _run never reads a stale self._daemon_loop
        # (which could be overwritten if _start_daemon is called again quickly).
        loop = asyncio.new_event_loop()
        self._daemon_loop = loop

        # Redirect daemon stdout + stderr to a persistent log file so any
        # error during enrollment or WS connect is visible after the fact.
        log_path = Path.home() / ".dispatch" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def _run() -> None:
            asyncio.set_event_loop(loop)
            log_file = open(log_path, "w", buffering=1)  # line-buffered
            log_file.write(
                f"=== daemon start @ broker={self.config.broker} "
                f"port={self.config.local_port} ===\n"
            )
            log_file.flush()
            import sys as _sys
            _sys.stdout = log_file
            _sys.stderr = log_file
            try:
                loop.run_until_complete(self._daemon_main())
            except (asyncio.CancelledError, RuntimeError) as e:
                log_file.write(f"=== daemon stopped: {type(e).__name__} ===\n")
            except Exception:
                import traceback
                log_file.write("=== daemon crashed ===\n")
                traceback.print_exc(file=log_file)
                self._set_status(ICON_ERROR, "Crashed — see ~/.dispatch/daemon.log")
            finally:
                log_file.flush()
                try: log_file.close()
                except Exception: pass

        t = threading.Thread(target=_run, daemon=True, name="dispatch-daemon")
        self._daemon_thread = t
        t.start()
        self._set_status(ICON_BUSY, "Connecting to broker…")

    def _restart_daemon(self) -> None:
        """Cancel the running session and restart with the current config.

        Safe to call from any thread. Used when credentials change (new sign-in)
        or when the user signs out from the web UI.
        """
        loop = self._daemon_loop
        if loop is not None and loop.is_running():
            # Cancel every task — causes run_session to exit and the broker WS
            # to close, which updates the status badge.
            loop.call_soon_threadsafe(
                lambda: [t.cancel() for t in asyncio.all_tasks(loop)]
            )
        self._daemon_loop = None
        rumps.Timer(self._delayed_daemon_start, 1.5).start()

    def _delayed_daemon_start(self, timer: rumps.Timer) -> None:
        timer.stop()
        # Wait for the old thread to fully exit before binding a new port.
        t = getattr(self, "_daemon_thread", None)
        if t is not None and t.is_alive():
            rumps.Timer(self._delayed_daemon_start, 0.5).start()
            return
        # Reload config from disk: sign-out wrote to ~/.dispatch/config.json
        # but our in-memory copy is stale. Without this we'd happily restart
        # the daemon with the just-deleted token.
        self.config = Config.load()
        if self.config.is_complete():
            self._start_daemon()
        else:
            self._set_status(ICON_ERROR, "Signed out — sign in at the broker")

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

        def on_notification(title: str, subtitle: str, message: str) -> None:
            self._on_main(lambda: rumps.notification(
                title=title, subtitle=subtitle, message=message,
            ))

        # Called by the web UI sign-out endpoint so the tray immediately
        # reflects the signed-out state and stops the broker WS.
        def on_signout() -> None:
            self._on_main(self._restart_daemon)

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
                rc = await run_session(
                    args,
                    on_status=on_status,
                    on_notification=on_notification,
                    on_signout=on_signout,
                )
                if rc == 7:
                    # Broker told us the user signed out. Stop reconnecting
                    # and drop in-memory credentials so the tray reflects it.
                    self.config = Config.load()
                    self._set_status(ICON_ERROR, "Signed out at broker")
                    self._on_main(lambda: rumps.notification(
                        title="Dispatch — signed out",
                        subtitle="You signed out at the broker.",
                        message="Open Broker to sign in again.",
                    ))
                    return
                if rc == 0:
                    backoff = 2
                    self._set_status(ICON_BUSY, "Reconnecting…")
                else:
                    self._set_status(
                        ICON_ERROR, f"Daemon exited with code {rc}. Retrying…"
                    )
            except asyncio.CancelledError:
                raise  # propagate — _restart_daemon or quit_app is stopping us
            except Exception:
                import traceback
                Path("/tmp/dispatch_daemon_retry.log").write_text(traceback.format_exc())
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
        self._open_inbox_when_ready(attempts=20)

    def _open_inbox_when_ready(self, attempts: int) -> None:
        """Poll the local server every 0.5s and open the inbox window as
        soon as it answers. If it never comes up (daemon failed to start),
        show a clear alert instead of a blank window."""
        import urllib.request as _ur
        port = self.config.local_port

        def _try(timer: rumps.Timer) -> None:
            nonlocal attempts
            try:
                _ur.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5)
            except Exception:
                attempts -= 1
                if attempts <= 0:
                    timer.stop()
                    rumps.alert(
                        title="Dispatch — daemon not responding",
                        message=(
                            "The local server didn't start on port "
                            f"{port}. Check the menu bar status — if it "
                            "says 'Daemon error', try Sign out and sign "
                            "in again from the broker page."
                        ),
                        ok="OK",
                    )
                return
            timer.stop()
            from dispatch.daemon.local_app import read_local_token
            token = read_local_token()
            suffix = f"#t={token}" if token else ""
            open_native_window(
                f"http://127.0.0.1:{port}/{suffix}",
                title="Dispatch — Inbox",
            )

        rumps.Timer(_try, 0.5).start()

    def open_broker_ui(self, _: rumps.MenuItem | None) -> None:
        target = self.config.broker or BROKER_URL
        webbrowser.open(target)

    def view_log(self, _: rumps.MenuItem | None) -> None:
        """Open the daemon log file in Console.app so the user can see exactly
        why the daemon is stuck or crashing."""
        import subprocess
        log_path = Path.home() / ".dispatch" / "daemon.log"
        if not log_path.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("(no log yet — daemon hasn't started)\n")
        subprocess.Popen(["open", "-a", "Console", str(log_path)])

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

        broker       = (params.get("broker") or [""])[0].rstrip("/")
        token        = (params.get("token") or [""])[0]
        api_key      = (params.get("api_key") or [""])[0]
        pending_invite = bool((params.get("invite") or [""])[0])
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
        else:
            # Credentials changed (re-sign-in after sign-out, or token refresh).
            # Restart the daemon so it picks up the new token immediately.
            self._restart_daemon()

        invite_msg = ("Open Inbox → People to accept it." if pending_invite else
                      "Click the menu bar icon → Open Inbox.")
        rumps.notification(
            title="Dispatch is configured",
            subtitle="Signed in. Daemon starting…",
            message=invite_msg,
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
