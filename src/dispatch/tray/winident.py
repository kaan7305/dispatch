"""Windows application identity: AUMID, toast registration, dispatch:// scheme.

macOS gets all of this from the ``~/Applications/Dispatch.app`` wrapper bundle
(see tray/bundle.py): a bundle identifier makes notifications attributable, and
``CFBundleURLTypes`` in its Info.plist is what makes the browser hand
``dispatch://configure?...`` to the tray after Clerk sign-in — the entire
no-terminal onboarding path.

Windows has no bundle. The equivalents are registry-backed and have to be
written explicitly:

* An **AppUserModelID** identifies the app to the shell. Without one, a toast
  raised by a Python process is attributed to whatever host raised it (usually
  "Windows PowerShell"), and Windows may drop it entirely. Registering the
  AUMID under ``HKCU\\Software\\Classes\\AppUserModelId`` gives the toast a
  name and an icon, so notifications say "Dispatch".
* A **URL protocol** handler under ``HKCU\\Software\\Classes\\dispatch`` is the
  Info.plist ``CFBundleURLTypes`` equivalent, and restores deep linking.

Everything here is per-user (HKCU), so none of it needs administrator rights —
which matters, because requiring elevation to install would be a worse
experience than macOS asks for, not an equal one.
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("dispatch.tray.winident")

# Reverse-DNS-ish and stable. Changing it strands every toast already in the
# Action Center and orphans the registry entries, so it is effectively an
# identifier of record.
APP_ID = "Dispatch.Agent.Tray"
APP_NAME = "Dispatch"
URL_SCHEME = "dispatch"

_IS_WINDOWS = sys.platform == "win32"


def app_data_dir() -> Path:
    """Per-user application data (``%LOCALAPPDATA%\\Dispatch``).

    The Windows counterpart of ``~/Library/Application Support/Dispatch``.
    LOCALAPPDATA rather than APPDATA: this holds a generated icon and, later, a
    vendored runtime — machine-local caches that should not follow a roaming
    profile across machines.
    """
    import os

    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Dispatch"


def icon_path() -> Path:
    return app_data_dir() / "dispatch.ico"


def ensure_icon() -> Path | None:
    """Make sure a .ico exists on disk for the shell to reference.

    The registry stores a *path* to an icon, not the image, so the toast
    registration and the Start-menu shortcut both need a real file.
    """
    path = icon_path()
    try:
        if path.exists() and path.stat().st_size > 0:
            return path
        from dispatch.tray.icons import write_ico

        return write_ico(path, "ok")
    except Exception:
        logger.debug("could not generate tray icon", exc_info=True)
        return None


def tray_executable() -> str:
    """The command the shell should run for autostart and deep links.

    Prefers the installed ``dispatch-tray`` launcher. When frozen, that is the
    executable itself.
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    found = shutil.which("dispatch-tray")
    if found:
        return found
    # Last resort: run the module through this interpreter. pythonw avoids a
    # console window flashing on every deep link.
    exe = sys.executable
    pythonw = Path(exe).with_name("pythonw.exe")
    if pythonw.exists():
        exe = str(pythonw)
    return f'"{exe}" -m dispatch.tray'


def set_process_app_id(app_id: str = APP_ID) -> bool:
    """Tell the shell which app this process is, for the current process only.

    Must happen before the first window or notification, otherwise Windows has
    already inferred an identity from the executable and will keep using it.
    """
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        logger.debug("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)
        return False


def register_toast_identity() -> bool:
    """Register the AUMID so toasts are attributed to Dispatch.

    Idempotent. Returns whether the registry now describes us.
    """
    if not _IS_WINDOWS:
        return False
    import winreg

    icon = ensure_icon()
    try:
        key_path = rf"Software\Classes\AppUserModelId\{APP_ID}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            if icon is not None:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
            # Without this a toast from a non-packaged app can be suppressed
            # entirely rather than shown without a badge.
            winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        return True
    except OSError:
        logger.debug("could not register toast identity", exc_info=True)
        return False


def register_url_scheme(command: str | None = None) -> bool:
    """Register ``dispatch://`` so the broker's install page can deep-link us.

    This is what makes the no-terminal onboarding work: after signing in with
    Google, the page opens ``dispatch://configure?broker=...&token=...`` and
    the shell hands it to the tray, which writes the config and starts the
    daemon. Without it that link silently does nothing on Windows and the user
    is stuck at a page telling them they are signed in.
    """
    if not _IS_WINDOWS:
        return False
    import winreg

    exe = command or tray_executable()
    # A bare path needs quoting; tray_executable() already quotes the
    # interpreter form, so only wrap when it is a plain path.
    launch = exe if exe.startswith('"') else f'"{exe}"'
    icon = ensure_icon()
    try:
        base = rf"Software\Classes\{URL_SCHEME}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f"URL:{APP_NAME} Protocol")
            # The presence of this value — not its content — is what marks the
            # key as a protocol handler.
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        if icon is not None:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, rf"{base}\DefaultIcon", 0, winreg.KEY_WRITE
            ) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f"{icon},0")
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, rf"{base}\shell\open\command", 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f'{launch} "%1"')
        return True
    except OSError:
        logger.debug("could not register dispatch:// scheme", exc_info=True)
        return False


def url_scheme_registered() -> bool:
    if not _IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, rf"Software\Classes\{URL_SCHEME}\shell\open\command"
        ) as key:
            value, _ = winreg.QueryValueEx(key, None)
            return bool(value)
    except OSError:
        return False


def unregister() -> None:
    """Remove both registrations. Best-effort; used by an uninstall path."""
    if not _IS_WINDOWS:
        return
    import winreg

    def _rm_tree(root, path: str) -> None:
        try:
            with winreg.OpenKey(root, path) as key:
                while True:
                    try:
                        child = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    _rm_tree(root, f"{path}\\{child}")
            winreg.DeleteKey(root, path)
        except OSError:
            pass

    _rm_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{URL_SCHEME}")
    _rm_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}")


def ensure_registered() -> None:
    """Everything the shell needs to know about us. Called at tray startup."""
    if not _IS_WINDOWS:
        return
    set_process_app_id()
    register_toast_identity()
    if not url_scheme_registered():
        register_url_scheme()
