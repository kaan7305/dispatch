"""Native macOS windows using WKWebView.

open_native_window() must be called from the main thread.
schedule_window()    can be called from any thread — it posts to the main thread.
"""
from __future__ import annotations

import rumps
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyRegular,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSMakeRect,
    NSURL,
    NSURLRequest,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from WebKit import WKWebView

# Strong references — prevents Python GC from destroying live windows.
_open_windows: list[NSWindow] = []

_STYLE = (
    NSWindowStyleMaskTitled
    | NSWindowStyleMaskClosable
    | NSWindowStyleMaskResizable
    | NSWindowStyleMaskMiniaturizable
)


def open_native_window(url: str, title: str = "Dispatch", width: int = 1000, height: int = 680) -> None:
    """Open a native Mac window with an embedded web view. Main thread only."""
    try:
        win_frame = NSMakeRect(160, 160, width, height)
        web_frame = NSMakeRect(0, 0, width, height)

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            win_frame, _STYLE, NSBackingStoreBuffered, False
        )
        win.setTitle_(title)
        win.setReleasedWhenClosed_(False)

        web = WKWebView.alloc().initWithFrame_(web_frame)
        web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

        win.setContentView_(web)

        # Temporarily switch from accessory (menu-bar-only) to regular so the
        # window actually comes to front on screen.
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

        _open_windows.append(win)
    except Exception as exc:
        from pathlib import Path
        Path("/tmp/dispatch_window.log").write_text(str(exc))


def schedule_window(url: str, title: str = "Dispatch") -> None:
    """Thread-safe: schedules open_native_window on the main run-loop."""
    def _open(timer: rumps.Timer) -> None:
        timer.stop()
        open_native_window(url, title)

    rumps.Timer(_open, 0.05).start()
