"""Auto-start at login via macOS LaunchAgents.

We write a plist to ~/Library/LaunchAgents/com.dispatch.tray.plist that
runs the installed dispatch-tray binary (or the bundled .app's executable
in a PyInstaller-frozen distribution) on user login.

We deliberately avoid SMAppService here because it requires the app to be
code-signed + notarized + Login Items registered against a bundled main
app — too heavy for the dev/personal-use phase. The LaunchAgent route
works for everyone today, including bare `pip install`.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.dispatch.tray"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _program_arguments() -> list[str]:
    """argv to autostart the tray.

    Preferred: the ~/Applications/Dispatch.app wrapper bundle — running
    inside a registered bundle is what lets UNUserNotificationCenter show
    the permission prompt and deliver banners (see tray/bundle.py).
    Frozen (PyInstaller .app): sys.executable.
    Fallback: the `dispatch-tray` script if on PATH.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    try:
        from dispatch.tray.bundle import bundle_program_arguments
        args = bundle_program_arguments()
        if args:
            return args
    except Exception:
        pass  # non-framework python, sandboxed FS, … — use the bare script
    found = shutil.which("dispatch-tray")
    return [found or sys.executable]


def is_enabled() -> bool:
    return PLIST_PATH.exists()


def enable() -> None:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist: dict = {
        "Label": LABEL,
        "ProgramArguments": _program_arguments(),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(Path.home() / ".dispatch" / "tray.log"),
        "StandardErrorPath": str(Path.home() / ".dispatch" / "tray.log"),
        "ProcessType": "Interactive",
    }
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(plist, fh)
    # Best-effort load; ignore launchctl exit code so a missing launchctl
    # (CI, container) doesn't break the flow.
    subprocess.run(
        ["launchctl", "load", "-w", str(PLIST_PATH)],
        check=False, capture_output=True,
    )


def disable() -> None:
    if not PLIST_PATH.exists():
        return
    subprocess.run(
        ["launchctl", "unload", "-w", str(PLIST_PATH)],
        check=False, capture_output=True,
    )
    try:
        os.remove(PLIST_PATH)
    except FileNotFoundError:
        pass
