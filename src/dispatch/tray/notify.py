"""User-facing notifications for the tray app.

A notification is not decoration here: it is how a recipient learns that
someone has sent them a dispatch, and — via its click target — how they get to
the approval that unblocks it. A dropped notification is a dispatch that sits
unanswered until it expires.

Delivery is layered, per platform.

macOS:
  1. UNUserNotificationCenter — the modern API. Works because the tray runs
     from the framework Python.app bundle (a real bundle, so the center
     resolves), and it's the only path that gives us click-through: tapping
     the banner opens the Dispatch window on the dispatch that caused it.
  2. osascript `display notification` — fallback when the UN center is
     unavailable (no bundle, authorization denied, missing pyobjc bindings).
     Fire-and-forget; clicks do nothing.

rumps.notification (NSUserNotificationCenter) is deliberately not used: it
was deprecated in 10.14 and silently drops notifications on modern macOS.

Windows:
  1. A real toast, raised through WinRT with our registered AppUserModelID so
     it is attributed to Dispatch rather than to the host process, and
     carrying a ``dispatch://open?d=<id>`` activation so clicking it opens the
     inbox on that dispatch — the same click-through macOS gets.
  2. A notification-area balloon, if the tray has offered one as a sink.
     Always available, no click routing.

Until this module had a Windows arm, every call here succeeded and delivered
nothing: the UN import is guarded, so `_HAVE_UN` was simply False and `send()`
fell through to an `osascript` binary that does not exist on Windows.

setup() must be called once, on the main thread, before send().
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Click callback: receives the dispatch_id the notification carried (or None).
_on_click: Callable[[str | None], None] | None = None
_center = None        # UNUserNotificationCenter, once authorized
_delegate = None      # strong ref — the center holds its delegate weakly

# Windows only: a fallback sink the tray installs so notifications still
# appear if the WinRT toast path is unavailable (pystray's balloon).
_balloon: Callable[[str, str], None] | None = None

try:
    import UserNotifications as UN
    from Foundation import NSObject

    class _UNDelegate(NSObject):
        """Click-through + show-while-foreground for our notifications."""

        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            self, _center, response, completion_handler
        ):
            dispatch_id: str | None = None
            try:
                info = response.notification().request().content().userInfo()
                raw = info.objectForKey_("dispatch_id") if info is not None else None
                dispatch_id = str(raw) if raw else None
            except Exception:
                logger.exception("could not read notification userInfo")
            cb = _on_click
            if cb is not None:
                try:
                    cb(dispatch_id)
                except Exception:
                    logger.exception("notification click handler failed")
            completion_handler()

        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
            self, _center, _notification, completion_handler
        ):
            # Show banners even while the app is frontmost.
            completion_handler(
                UN.UNNotificationPresentationOptionBanner
                | UN.UNNotificationPresentationOptionSound
            )

    _HAVE_UN = True
except Exception:  # bindings missing or no AppKit at all (headless install)
    _HAVE_UN = False


def set_balloon_sink(sink: Callable[[str, str], None] | None) -> None:
    """Offer a last-resort notifier, called as ``sink(title, message)``.

    The Windows tray passes pystray's balloon here. It is only used when the
    toast path fails, so a machine with notifications disabled at the OS level
    still surfaces something in the notification area.
    """
    global _balloon
    _balloon = sink


def setup(on_click: Callable[[str | None], None] | None = None) -> None:
    """Wire the notification backend + click handler. Main thread, once, at
    app start. Safe to call when no rich backend is available — send() then
    falls back to whatever the platform can still manage."""
    global _on_click, _center, _delegate
    _on_click = on_click
    if _IS_WINDOWS:
        # Registering the AUMID is what makes a toast say "Dispatch" instead of
        # naming the host process, and on some builds is the difference between
        # a toast appearing and being dropped without error.
        try:
            from dispatch.tray import winident

            winident.ensure_registered()
        except Exception:
            logger.debug("windows notification identity setup failed", exc_info=True)
        return
    if not _HAVE_UN:
        return
    try:
        center = UN.UNUserNotificationCenter.currentNotificationCenter()
        _delegate = _UNDelegate.alloc().init()
        center.setDelegate_(_delegate)

        def _granted(granted: bool, error) -> None:
            global _center
            if granted:
                _center = center
            else:
                logger.warning("notification authorization denied (%s)", error)

        center.requestAuthorizationWithOptions_completionHandler_(
            UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
            _granted,
        )
    except Exception:
        # Typically "bundleProxyForCurrentProcess is nil" — not a bundle.
        logger.exception("UNUserNotificationCenter unavailable; using osascript")


def send(title: str, subtitle: str, message: str, dispatch_id: str | None = None) -> None:
    """Post a notification. Thread-safe; never raises."""
    if _IS_WINDOWS:
        _windows_send(title, subtitle, message, dispatch_id)
        return
    if _center is not None:
        try:
            content = UN.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setSubtitle_(subtitle)
            content.setBody_(message)
            content.setSound_(UN.UNNotificationSound.defaultSound())
            if dispatch_id:
                content.setUserInfo_({"dispatch_id": str(dispatch_id)})
            request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                str(uuid4()), content, None
            )
            _center.addNotificationRequest_withCompletionHandler_(request, None)
            return
        except Exception:
            logger.exception("UN notification failed; falling back to osascript")
    _osascript(title, subtitle, message)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _toast_xml(title: str, subtitle: str, message: str, dispatch_id: str | None) -> str:
    """The toast payload.

    ``activationType="protocol"`` with a ``dispatch://`` launch is how a click
    reaches us: Windows does not call back into the process that raised the
    toast (it may not even be running by then), it re-launches the registered
    protocol handler. That is why tray/winident.py registers the scheme —
    without it a toast still appears but clicking it does nothing.
    """
    lines = [t for t in (subtitle, message) if t]
    body = "".join(f"<text>{_xml_escape(t)}</text>" for t in lines)
    launch = "dispatch://open"
    if dispatch_id:
        launch = f"dispatch://open?d={_xml_escape(str(dispatch_id))}"
    return (
        f'<toast launch="{launch}" activationType="protocol">'
        f'<visual><binding template="ToastGeneric">'
        f"<text>{_xml_escape(title)}</text>{body}"
        f"</binding></visual>"
        f'<audio src="ms-winsoundevent:Notification.Default"/>'
        f"</toast>"
    )


def _windows_send(
    title: str, subtitle: str, message: str, dispatch_id: str | None
) -> None:
    """Raise a Windows toast, falling back to a balloon.

    The toast is raised by a short-lived PowerShell process rather than through
    an in-process WinRT binding, because every Python WinRT package is an extra
    (and platform-pinned) dependency, and the daemon must not gain one just to
    show a banner. The process is spawned detached and never waited on:
    notifications are raised from the daemon's event loop, and blocking it for
    a few hundred milliseconds per dispatch would be a far worse bug than an
    ugly banner.
    """
    from dispatch.tray.winident import APP_ID

    xml = _toast_xml(title, subtitle, message, dispatch_id)
    # A here-string keeps the XML literal — no PowerShell interpolation of the
    # $ and backtick characters a task title can easily contain. The closing
    # '@ must be at column 0.
    script = (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,"
        "ContentType=WindowsRuntime]>$null;"
        "$d=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$d.LoadXml(@'\n" + xml + "\n'@);"
        "$t=New-Object Windows.UI.Notifications.ToastNotification $d;"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($t)"
    )

    try:
        from dispatch.shared.proc import spawn_detached

        spawn_detached(
            [
                _powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden",
                "-Command", script,
            ]
        )
        return
    except Exception:
        logger.debug("toast notification failed; trying balloon", exc_info=True)

    sink = _balloon
    if sink is not None:
        try:
            sink(title, " — ".join(t for t in (subtitle, message) if t))
        except Exception:
            logger.debug("balloon notification failed", exc_info=True)


def _powershell() -> str:
    """An absolute path to Windows PowerShell.

    Resolved rather than trusted from PATH: a user who has shadowed
    ``powershell`` with something else should not silently lose notifications,
    and a detached process with a scrubbed PATH may not find it at all.
    """
    import os

    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.exists(candidate):
        return candidate
    import shutil as _shutil

    return _shutil.which("powershell") or "powershell.exe"


def _osascript(title: str, subtitle: str, message: str) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(message)}" '
        f'with title "{esc(title)}" subtitle "{esc(subtitle)}" '
        f'sound name "default"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=10, check=False,
        )
    except Exception:
        logger.exception("osascript notification failed")
