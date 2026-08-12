"""Dispatch notification-area app for Windows.

The macOS counterpart (tray/app.py) is a rumps menu-bar app; this is the same
product surface built on pystray, sharing the platform-neutral supervisor in
tray/supervisor.py. What it provides, one for one with macOS:

  * a persistent indicator whose state says whether the daemon is online,
  * a hosted daemon — the tray *is* the always-on process, not a launcher,
  * notifications, with click-through to the dispatch that caused them,
  * an inbox window,
  * autostart at login,
  * the ``dispatch://configure`` deep link that completes browser sign-in,
  * one-click reload when ``dispatch update`` has installed newer code.

Two things are shaped differently by the platform rather than by choice:

**Status is an icon, not text.** A menu bar item can hold a title, so macOS
says "⬡ Dispatch" / "◌ Dispatch" / "⚠ Dispatch" in words. The notification area
is a 16px bitmap, so the same three states are colour and shape (tray/icons.py)
plus a tooltip, and the detail line moves into the menu.

**Sleep/wake is inferred, not subscribed.** macOS gets
``NSWorkspaceDidWakeNotification``. The Windows equivalent needs a
message-only window pumping WM_POWERBROADCAST, which means owning a second
event loop next to pystray's. Instead the scheduler watches for wall-clock time
jumping much further than the interval it slept for, which detects sleep,
hibernate and lid-close alike, and costs one comparison a second.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from dispatch.tray import notify, winident
from dispatch.tray.autostart import disable as autostart_disable
from dispatch.tray.autostart import enable as autostart_enable
from dispatch.tray.autostart import is_enabled as autostart_is_enabled
from dispatch.tray.config import Config
from dispatch.tray.supervisor import (
    STATE_BUSY,
    STATE_ERROR,
    STATE_OK,
    DaemonSupervisor,
    Scheduler,
)

logger = logging.getLogger("dispatch.tray.win")

# An explicitly-set DISPATCH_BROKER overrides saved config; the default is only
# a last resort for a first run with no config yet.
BROKER_ENV = (os.environ.get("DISPATCH_BROKER") or "").rstrip("/")
DEFAULT_BROKER_URL = "https://dispatch-production-99d1.up.railway.app"
BROKER_URL = BROKER_ENV or DEFAULT_BROKER_URL

# A second launch (from a dispatch:// deep link, say) cannot show a second
# icon, so it leaves the URL here and exits; the running tray picks it up.
PENDING_URL = Path.home() / ".dispatch" / "pending-url"

_MUTEX_NAME = "Local\\dispatch-tray-single-instance"
_mutex_handle = None


def _acquire_single_instance() -> bool:
    """Claim the one-tray-per-session slot.

    A named kernel mutex rather than a lock file: the macOS tray uses flock,
    which does not exist here, and the naive port (create a file, check it
    exists) leaks on a crash and wedges every future launch. Windows releases a
    mutex when the owning process dies, however it dies.

    The ``Local\\`` prefix scopes it to the login session, so two users on the
    same machine — or two RDP sessions — each get their own tray, which is the
    behaviour they should get.
    """
    global _mutex_handle
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return True  # cannot tell; better a second icon than no tray at all
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle  # held for the process lifetime
    return True


def tray_already_running() -> bool:
    """Is another tray holding the single-instance mutex?

    Cheaper and far more reliable than scanning the process table, which is
    what the CLI used to attempt with ``pgrep`` — a binary Windows does not
    have, so the check always answered "no".
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, _MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def alert(title: str, message: str) -> None:
    """A modal the user cannot miss — the rumps.alert equivalent.

    Shown on a worker thread so a blocking message box never stalls the tray's
    own loop, which would freeze the icon and its menu.
    """
    def _show() -> None:
        try:
            import ctypes

            MB_OK = 0x0
            MB_ICONINFORMATION = 0x40
            MB_SETFOREGROUND = 0x10000
            MB_TOPMOST = 0x40000
            ctypes.windll.user32.MessageBoxW(
                None, str(message), str(title),
                MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST,
            )
        except Exception:
            logger.warning("%s: %s", title, message)

    threading.Thread(target=_show, daemon=True, name="dispatch-alert").start()


class DispatchTray:
    def __init__(self) -> None:
        import pystray

        self._pystray = pystray
        self.config = Config.load()
        if BROKER_ENV:
            self.config.broker = BROKER_ENV

        self.scheduler = Scheduler()
        self.supervisor = DaemonSupervisor(
            on_status=self._set_status,
            on_notify=self._notify,
            scheduler=self.scheduler,
        )
        self.supervisor.config = self.config

        self._status_text = "Starting…"
        self._state = STATE_BUSY
        self._icon = None
        self._last_tick = time.time()

        winident.ensure_registered()
        notify.setup(on_click=self._on_notification_click)

    # -- menu --------------------------------------------------------------

    def _build_menu(self):
        pystray = self._pystray
        item = pystray.MenuItem

        def update_title(_item):
            return (
                "⬇ Reload to apply update"
                if self.supervisor.update_pending
                else "Up to date"
            )

        return pystray.Menu(
            # A callback-less item renders greyed: a status line, not a button.
            item(lambda _i: self._status_text, None, enabled=False),
            item(update_title, self._reload_for_update,
                 enabled=lambda _i: self.supervisor.update_pending),
            pystray.Menu.SEPARATOR,
            item("Open Inbox", self._open_inbox, default=True),
            item("Open Broker", self._open_broker),
            pystray.Menu.SEPARATOR,
            item("Start at login", self._toggle_autostart,
                 checked=lambda _i: autostart_is_enabled()),
            item("Account…", self._show_account),
            item("View Log", self._view_log),
            pystray.Menu.SEPARATOR,
            item("Quit", self._quit),
        )

    # -- status ------------------------------------------------------------

    def _set_status(self, state: str, detail: str) -> None:
        self._state = state
        self._status_text = detail
        icon = self._icon
        if icon is None:
            return
        try:
            from dispatch.tray.icons import render

            icon.icon = render(state)  # type: ignore[assignment]
            # The tooltip is the only place the detail line is visible without
            # opening the menu, so it carries the same text macOS puts in the
            # menu bar.
            icon.title = f"Dispatch — {detail}"
            icon.update_menu()
        except Exception:
            logger.debug("could not update tray icon", exc_info=True)

    def _notify(
        self, title: str, subtitle: str, message: str, dispatch_id: str | None = None
    ) -> None:
        notify.send(title, subtitle, message, dispatch_id=dispatch_id)

    def _on_notification_click(self, dispatch_id: str | None) -> None:
        self._open_inbox_when_ready(dispatch_id=dispatch_id)

    # -- menu actions ------------------------------------------------------

    def _open_inbox(self, _icon=None, _item=None) -> None:
        if not self.config.is_complete():
            alert(
                "Dispatch — not signed in",
                "Run the install command first, then restart Dispatch.",
            )
            return
        self._open_inbox_when_ready()

    def _open_inbox_when_ready(
        self, attempts: int = 20, dispatch_id: str | None = None
    ) -> None:
        """Open the UI as soon as the local server answers.

        The window is served *by* the daemon, so opening it before the daemon
        binds its port would show a connection error rather than an inbox.
        """
        port = self.config.local_port

        def _try(remaining: int) -> None:
            from dispatch.desktop import app_url, daemon_alive, open_window

            if not daemon_alive(port, timeout=0.5):
                if remaining <= 1:
                    alert(
                        "Dispatch — daemon not responding",
                        f"The local server didn't start on port {port}.\n\n"
                        "Check the tray status; if it says 'Daemon error', "
                        "sign out and back in from the broker page.",
                    )
                    return
                self.scheduler.call_later(0.5, lambda: _try(remaining - 1))
                return
            try:
                from dispatch.daemon.local_app import read_local_token

                token = read_local_token() or ""
            except Exception:
                token = ""
            try:
                open_window(app_url(port, token, dispatch_id))
            except Exception:
                logger.exception("could not open the inbox window")

        self.scheduler.call_later(0.0, lambda: _try(attempts))

    def _open_broker(self, _icon=None, _item=None) -> None:
        webbrowser.open(self.config.broker or BROKER_URL)

    def _toggle_autostart(self, _icon=None, _item=None) -> None:
        try:
            if autostart_is_enabled():
                autostart_disable()
            else:
                autostart_enable()
        except OSError as e:
            alert("Dispatch", f"Could not change the login setting:\n{e}")
        if self._icon is not None:
            self._icon.update_menu()

    def _show_account(self, _icon=None, _item=None) -> None:
        alert(
            "Dispatch — account",
            f"Signed in as: {self.supervisor.account_label()}\n"
            f"Broker: {self.config.broker or '(unset)'}\n"
            f"Local UI: http://127.0.0.1:{self.config.local_port}",
        )

    def _view_log(self, _icon=None, _item=None) -> None:
        from dispatch.shared.proc import open_external

        log_path = Path.home() / ".dispatch" / "daemon.log"
        if not log_path.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "(no log yet — daemon hasn't started)\n", encoding="utf-8"
            )
        try:
            open_external(str(log_path))
        except OSError as e:
            alert("Dispatch", f"Could not open the log:\n{e}")

    def _reload_for_update(self, _icon=None, _item=None) -> None:
        """Re-exec onto freshly-installed code.

        Restarting the daemon thread is not enough: Python keeps the old
        modules in memory, so only a new process picks up new code. And this
        cannot use os.execv, which on Windows is not a real exec — the CRT
        spawns a new process and terminates the caller *asynchronously*, so
        both briefly exist and the new one loses the race for the port and the
        single-instance mutex. Spawn, release, exit, in that order.
        """
        if not self.supervisor.update_pending:
            self._notify(
                "Dispatch", "Already up to date.",
                "No newer code has been installed since this tray started.",
            )
            return
        from dispatch.shared.proc import spawn_detached

        exe = winident.tray_executable()
        cmd = [exe] if not exe.startswith('"') else __import__("shlex").split(exe)
        self.supervisor.stop()
        global _mutex_handle
        try:
            if _mutex_handle is not None:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(_mutex_handle)
                _mutex_handle = None
        except Exception:
            pass
        try:
            spawn_detached(cmd)
        except OSError:
            logger.exception("could not relaunch the tray")
        if self._icon is not None:
            self._icon.stop()

    def _quit(self, _icon=None, _item=None) -> None:
        self.supervisor.stop()
        if self._icon is not None:
            self._icon.stop()

    # -- deep links --------------------------------------------------------

    def handle_url(self, url: str) -> None:
        """Handle ``dispatch://configure?...`` and ``dispatch://open?d=...``.

        The configure form is the whole no-terminal onboarding: the broker's
        install page, after Google sign-in, opens this URL with the credentials
        so the user never sees a shell.
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return
        if parsed.scheme != "dispatch":
            return
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.netloc == "open":
            self._open_inbox_when_ready(dispatch_id=(params.get("d") or [None])[0])
            return
        if parsed.netloc != "configure":
            return

        broker = (params.get("broker") or [""])[0].rstrip("/")
        token = (params.get("token") or [""])[0]
        api_key = (params.get("api_key") or [""])[0]
        pending_invite = bool((params.get("invite") or [""])[0])
        if not (broker and token):
            alert(
                "Dispatch — bad install link",
                "The link was missing the broker URL or token. "
                "Try again from the install page.",
            )
            return

        was_complete = self.config.is_complete()
        self.config.broker = broker
        self.config.token = token
        if api_key:
            self.config.anthropic_api_key = api_key
        self.config.save()
        self.supervisor.config = self.config

        if not was_complete:
            self.supervisor.start()
        else:
            self.supervisor.restart()

        self._notify(
            "Dispatch is configured",
            "Signed in. Daemon starting…",
            "Open Inbox → People to accept it." if pending_invite
            else "Click the Dispatch tray icon → Open Inbox.",
        )

    def _drain_pending_url(self) -> None:
        """Pick up a URL a second launch left for us."""
        try:
            if not PENDING_URL.exists():
                return
            url = PENDING_URL.read_text(encoding="utf-8").strip()
            PENDING_URL.unlink(missing_ok=True)
        except OSError:
            return
        if url:
            self.handle_url(url)

    # -- periodic work -----------------------------------------------------

    def _tick(self) -> None:
        now = time.time()
        drift = now - self._last_tick
        self._last_tick = now
        # The scheduler asked for 2s. A far larger wall-clock gap means the
        # machine was suspended, and the broker socket is now a zombie the
        # keepalive will not notice for another ~10s.
        if drift > 60:
            logger.info("detected a %.0fs suspend; reconnecting", drift)
            self.supervisor.on_system_wake()
        self._drain_pending_url()

    def _poll_update(self) -> None:
        if self.supervisor.check_for_update():
            self._set_status(self._state, f"{self._status_text}  ·  update ready")
            if self._icon is not None:
                self._icon.update_menu()
            self._notify(
                "Dispatch — update installed",
                "Running on the old code until you reload.",
                "Click the Dispatch tray icon → Reload to apply update.",
            )

    # -- run ---------------------------------------------------------------

    def run(self, initial_url: str | None = None) -> None:
        import pystray

        from dispatch.tray.icons import render

        self._icon = pystray.Icon(
            "dispatch",
            icon=render(STATE_BUSY),
            title="Dispatch — starting…",
            menu=self._build_menu(),
        )
        # pystray's balloon is the fallback when a WinRT toast cannot be shown
        # (notifications disabled by policy, PowerShell blocked).
        notify.set_balloon_sink(
            lambda title, message: self._icon and self._icon.notify(message, title)
        )

        self.scheduler.start()
        self.scheduler.every(2.0, self._tick)
        self.scheduler.every(15.0, self._poll_update)

        def _startup() -> None:
            if initial_url:
                self.handle_url(initial_url)
                return
            if not self.config.is_complete():
                self._set_status(STATE_ERROR, "Not signed in — run the installer")
                alert(
                    "Dispatch is not signed in",
                    f"Visit {self.config.broker or BROKER_URL} in your browser, "
                    "sign in, and run the install command shown there.",
                )
                return
            self.supervisor.start()

        self.scheduler.call_later(0.3, _startup)
        self._icon.run()
        self.supervisor.stop()


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    url = ""
    for arg in sys.argv[1:]:
        if arg.startswith("dispatch://"):
            url = arg
            break

    if not _acquire_single_instance():
        # A tray is already up. If we were launched to service a deep link,
        # hand it over rather than dropping it, then get out of the way — a
        # second icon would be worse than no second process.
        if url:
            try:
                PENDING_URL.parent.mkdir(parents=True, exist_ok=True)
                PENDING_URL.write_text(url, encoding="utf-8")
            except OSError:
                pass
        else:
            sys.stderr.write(
                "dispatch-tray: another instance is already running; exiting.\n"
            )
        return 0

    try:
        DispatchTray().run(initial_url=url or None)
    except ImportError as e:
        sys.stderr.write(
            f"dispatch-tray: missing a tray dependency ({e}).\n"
            "Install it with:  pip install 'dispatch-agent[tray]'\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
