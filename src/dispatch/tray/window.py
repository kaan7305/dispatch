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
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSURL,
    NSURLRequest,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSObject
from WebKit import WKWebView

# Strong references — prevents Python GC from destroying live windows.
_open_windows: list[NSWindow] = []
_delegates: list[NSObject] = []

# Windows can't shrink below a usable layout (search bar + sidebar + detail).
_MIN_WIDTH, _MIN_HEIGHT = 860, 560

# WKNavigationActionPolicy values (not exported by every pyobjc build).
_POLICY_CANCEL, _POLICY_ALLOW = 0, 1


def _is_local(url) -> bool:
    host = str(url.host() or "")
    return host in ("127.0.0.1", "localhost", "::1")


class _WebDelegate(NSObject):
    """Routes anything that isn't the local app to the system browser.

    - Normal link clicks to external hosts: cancel the in-webview navigation
      and hand the URL to NSWorkspace (otherwise the SPA gets replaced by the
      external page — or, for sites refusing to load in a webview, nothing).
    - target=_blank / window.open: WKWebView silently drops these without a
      UI delegate; open them externally and return no new web view.
    """

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, _webview, action, handler):
        url = action.request().URL()
        scheme = str(url.scheme() or "").lower() if url is not None else ""
        if url is not None and scheme in ("http", "https") and not _is_local(url):
            NSWorkspace.sharedWorkspace().openURL_(url)
            handler(_POLICY_CANCEL)
            return
        handler(_POLICY_ALLOW)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
        self, _webview, _config, action, _features
    ):
        url = action.request().URL()
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_(url)
        return None

_STYLE = (
    NSWindowStyleMaskTitled
    | NSWindowStyleMaskClosable
    | NSWindowStyleMaskResizable
    | NSWindowStyleMaskMiniaturizable
)


def _install_edit_menu() -> None:
    """Accessory apps have no main menu, so Cmd+C/V/X/A never reach the
    web view. Install a minimal Edit menu bound to the standard selectors."""
    main_menu = NSApp.mainMenu()
    if main_menu is None:
        main_menu = NSMenu.alloc().init()
        # The first item of a main menu is always treated as the app menu.
        main_menu.addItem_(
            NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Dispatch", None, "")
        )
        NSApp.setMainMenu_(main_menu)
    if main_menu.itemWithTitle_("Edit") is not None:
        return

    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "Z"),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        edit_menu.addItem_(
            NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        )
    edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
    edit_item.setSubmenu_(edit_menu)
    main_menu.addItem_(edit_item)


def _find_open_window(title: str) -> NSWindow | None:
    """Return a still-visible window with this title, pruning closed ones."""
    alive: list[NSWindow] = []
    found: NSWindow | None = None
    for win in _open_windows:
        try:
            if not win.isVisible():
                continue
        except Exception:
            continue
        alive.append(win)
        if found is None and str(win.title()) == title:
            found = win
    _open_windows[:] = alive
    return found


def open_native_window(url: str, title: str = "Dispatch", width: int = 1000, height: int = 680) -> None:
    """Open a native Mac window with an embedded web view, or raise an
    existing window with the same title if one is already open. Main
    thread only."""
    try:
        _install_edit_menu()
        existing = _find_open_window(title)
        if existing is not None:
            # Navigate to the current URL so a fresh token or a rebuilt dist
            # is always loaded, rather than showing a stale cached page.
            existing.contentView().loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
            )
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            existing.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            return

        win_frame = NSMakeRect(160, 160, width, height)
        web_frame = NSMakeRect(0, 0, width, height)

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            win_frame, _STYLE, NSBackingStoreBuffered, False
        )
        win.setTitle_(title)
        win.setReleasedWhenClosed_(False)
        win.setContentMinSize_(NSMakeSize(_MIN_WIDTH, _MIN_HEIGHT))

        web = WKWebView.alloc().initWithFrame_(web_frame)
        delegate = _WebDelegate.alloc().init()
        _delegates.append(delegate)
        web.setNavigationDelegate_(delegate)
        web.setUIDelegate_(delegate)
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
