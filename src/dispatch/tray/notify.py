"""User-facing macOS notifications for the tray app.

Delivery is layered:

  1. UNUserNotificationCenter — the modern API. Works because the tray runs
     from the framework Python.app bundle (a real bundle, so the center
     resolves), and it's the only path that gives us click-through: tapping
     the banner opens the Dispatch window on the dispatch that caused it.
  2. osascript `display notification` — fallback when the UN center is
     unavailable (no bundle, authorization denied, missing pyobjc bindings).
     Fire-and-forget; clicks do nothing.

rumps.notification (NSUserNotificationCenter) is deliberately not used: it
was deprecated in 10.14 and silently drops notifications on modern macOS.

setup() must be called once, on the main thread, before send().
"""
from __future__ import annotations

import logging
import subprocess
from typing import Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

# Click callback: receives the dispatch_id the notification carried (or None).
_on_click: Callable[[str | None], None] | None = None
_center = None        # UNUserNotificationCenter, once authorized
_delegate = None      # strong ref — the center holds its delegate weakly

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


def setup(on_click: Callable[[str | None], None] | None = None) -> None:
    """Wire the UN center + click handler. Main thread, once, at app start.
    Safe to call when UN is unavailable — send() then uses osascript."""
    global _on_click, _center, _delegate
    _on_click = on_click
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
