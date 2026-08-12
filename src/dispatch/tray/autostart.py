"""Start the tray at login.

macOS uses a LaunchAgent plist. We deliberately avoid SMAppService: it requires
the app to be code-signed + notarized + registered as a Login Item against a
bundled main app — too heavy for the dev/personal-use phase. The LaunchAgent
route works for everyone today, including a bare ``pip install``.

Windows uses the per-user ``Run`` registry key. A Startup-folder shortcut would
be the other option, but a .lnk has to be built through COM and can be written
successfully while pointing at nothing (the same class of silent failure the
desktop shortcut had); a registry value is a string, either right or absent.
Both surfaces are equally visible to the user — Task Manager's Startup tab
lists Run-key entries — and neither needs administrator rights.

Every function here is a dispatcher on ``sys.platform``. The previous version
was not: ``PLIST_PATH`` was computed at import as ``~/Library/LaunchAgents/…``
with no guard, so on Windows ``is_enabled()`` was permanently False and
``enable()`` cheerfully created a ``%USERPROFILE%\\Library\\LaunchAgents``
directory containing a plist nothing would ever read, then failed on a missing
``launchctl`` — reporting success for an autostart that did not exist.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.dispatch.tray"

# The Run-key value name on Windows. Stable: renaming it orphans the old entry
# and the tray would start twice.
RUN_VALUE = "Dispatch"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

_IS_WINDOWS = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"


def plist_path() -> Path:
    """The macOS LaunchAgent path. Meaningless elsewhere; kept as a function so
    importing this module on Windows does not compute a nonsense path."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# Retained for callers that referenced the old module constant.
PLIST_PATH = plist_path()


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


def _windows_command() -> str:
    """The command line Windows should run at login.

    Quoted because the interpreter path contains spaces on almost every real
    install (``C:\\Users\\First Last\\...``), and an unquoted Run value is
    split on the first space and silently fails to launch.
    """
    from dispatch.tray.winident import tray_executable

    exe = tray_executable()
    return exe if exe.startswith('"') else f'"{exe}"'


def is_enabled() -> bool:
    if _IS_WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, RUN_VALUE)
                return bool(value)
        except OSError:
            return False
    return plist_path().exists()


def enable() -> None:
    if _IS_WINDOWS:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _windows_command())
        return

    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    plist: dict = {
        "Label": LABEL,
        "ProgramArguments": _program_arguments(),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(Path.home() / ".dispatch" / "tray.log"),
        "StandardErrorPath": str(Path.home() / ".dispatch" / "tray.log"),
        "ProcessType": "Interactive",
    }
    with path.open("wb") as fh:
        plistlib.dump(plist, fh)
    # Best-effort load; ignore launchctl exit code so a missing launchctl
    # (CI, container) doesn't break the flow.
    subprocess.run(
        ["launchctl", "load", "-w", str(path)],
        check=False, capture_output=True,
    )


def disable() -> None:
    if _IS_WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass  # already absent
        return

    path = plist_path()
    if not path.exists():
        return
    subprocess.run(
        ["launchctl", "unload", "-w", str(path)],
        check=False, capture_output=True,
    )
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def describe() -> str:
    """Where autostart is configured, for `dispatch` to print."""
    if _IS_WINDOWS:
        return rf"HKCU\{_RUN_KEY}\{RUN_VALUE}"
    return str(plist_path())
