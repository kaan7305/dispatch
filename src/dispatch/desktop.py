"""Open the local desktop UI on demand, without going through the menu bar.

The "desktop app" is not a separate binary. The daemon serves a single-page
app on the loopback interface and the tray simply points a WKWebView at

    http://127.0.0.1:<local_port>/#t=<local token>[&d=<dispatch id>]

which means the menu bar is an accident of how the UI shipped, not a
requirement of it. That accident bites in two places: a full menu bar has no
room for another icon (macOS silently drops the ones that don't fit), and
Windows has no tray app at all — so on that platform the UI currently has no
door in front of it.

Everything here is therefore platform-neutral apart from two tables: where the
Chromium-family browsers live (``_CHROMIUM``) and what counts as a launcher the
OS will surface (``install_shortcut``). The window is an "app mode" browser
window — ``--app=<url>`` renders the page chromeless with its own Dock/taskbar
icon, which is as close to native as we get without shipping a webview per
platform, and it costs one flag instead of one implementation per OS.

The window runs in a dedicated browser profile under ``~/.dispatch``, for the
same reason ``dispatch.browser`` keeps one: the URL carries the loopback token
in its fragment, and a browser's history is forever. A dedicated profile keeps
that token out of the browser the user reads their email in.
"""
from __future__ import annotations

import os
import subprocess
import shutil
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from dispatch.shared.config import dispatch_home

# The shortcut is deliberately NOT called "Dispatch": ~/Applications/Dispatch.app
# is the tray's own wrapper bundle (dispatch.tray.bundle), and two bundles with
# one name is how you end up launching the wrong one from Spotlight forever.
SHORTCUT_NAME = "Dispatch Inbox"

WINDOW_SIZE = "1100,760"

# How long to wait for a daemon we just started to answer on loopback. The tray
# path is the slow one (it boots the daemon inside itself), and a cold start on
# a loaded machine is a few seconds.
DAEMON_WAIT_S = 25.0

_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def app_profile_dir() -> Path:
    return dispatch_home() / "app-profile"


# ---------------------------------------------------------------------------
# The URL
# ---------------------------------------------------------------------------


def app_url(port: int, token: str, dispatch_id: str | None = None) -> str:
    """The same URL the tray's "Open Inbox" builds.

    Both parameters live in the fragment, which never leaves the machine: it
    isn't sent to the server (not even to the loopback one), and the SPA's
    bootstrap reads it from ``location.hash``.
    """
    parts = []
    if token:
        parts.append(f"t={token}")
    if dispatch_id:
        parts.append(f"d={dispatch_id}")
    suffix = f"#{'&'.join(parts)}" if parts else ""
    return f"http://127.0.0.1:{port}/{suffix}"


# ---------------------------------------------------------------------------
# Is the daemon up?
# ---------------------------------------------------------------------------


def daemon_alive(port: int, timeout: float = 0.6) -> bool:
    """Does something answer on the loopback UI port?

    Deliberately unauthenticated and status-blind: the SPA's index is served
    without a token, so any HTTP response at all proves the daemon is serving.
    """
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # answered — that's all we asked
    except (urllib.error.URLError, OSError):
        return False


def wait_for_daemon(port: int, timeout: float = DAEMON_WAIT_S) -> bool:
    """Poll until the daemon serves, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_alive(port):
            return True
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

# Ordered by preference, and by what the platform guarantees: Edge is always
# present on Windows 10+, so that branch can promise a real app window even on
# a machine with nothing else installed.
_CHROMIUM: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "win32": (
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
}

# Linux ships browsers on PATH rather than at fixed paths.
_CHROMIUM_ON_PATH = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
)


def find_chromium() -> Path | None:
    """First Chromium-family browser on this machine, or None.

    Only Chromium implements ``--app``; Safari and Firefox have no equivalent,
    which is why the fallback is a plain tab rather than a worse window.
    """
    for raw in _CHROMIUM.get(sys.platform, ()):
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        # expandvars leaves unset %VARS% verbatim — those can't be a real path.
        if "%" not in str(path) and path.exists():
            return path
    for name in _CHROMIUM_ON_PATH:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _spawn_detached(cmd: list[str]) -> None:
    """Start a GUI process that outlives this CLI invocation.

    Closing the terminal must not take the window with it, so the child gets
    its own session (POSIX) / process group (Windows).
    """
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def open_window(url: str, *, prefer_browser: bool = False) -> tuple[str, Path | None]:
    """Show ``url``. Returns ("app", browser) or ("browser", None).

    The app-mode window gets its own profile directory, which also means it
    never merges into a running browser's window stack — you get a window,
    every time, instead of a tab somewhere behind 40 others.
    """
    if not prefer_browser:
        exe = find_chromium()
        if exe is not None:
            profile = app_profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            _spawn_detached([
                str(exe),
                f"--app={url}",
                f"--user-data-dir={profile}",
                f"--window-size={WINDOW_SIZE}",
                "--no-first-run",
                "--no-default-browser-check",
            ])
            return "app", exe
    webbrowser.open(url)
    return "browser", None


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------


def cli_executable() -> str:
    """Absolute path to the installed `dispatch` command.

    Baked into the shortcut at creation time: a Start-menu entry or .app runs
    with the OS's PATH, not the user's shell PATH, and pipx's bin directory is
    on neither by default.
    """
    found = shutil.which("dispatch")
    if found:
        return str(Path(found).resolve())
    # Running from a checkout or a renamed entry point — argv[0] still points
    # at something that works.
    return str(Path(sys.argv[0]).resolve())


def shortcut_path() -> Path:
    """Where this platform expects a user-installed launcher to live."""
    if sys.platform == "darwin":
        return Path.home() / "Applications" / f"{SHORTCUT_NAME}.app"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" /
                "Programs" / f"{SHORTCUT_NAME}.lnk")
    return Path.home() / ".local" / "share" / "applications" / "dispatch-inbox.desktop"


def install_shortcut() -> Path:
    """Create (or refresh) a launcher the OS's own search will find.

    All three platforms do the same thing — hand `dispatch open` to the
    application launcher — they just disagree about the file format.
    """
    target = shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    cli = cli_executable()

    if sys.platform == "darwin":
        _install_mac_app(target, cli)
    elif sys.platform == "win32":
        _install_windows_lnk(target, cli)
    else:
        _install_desktop_entry(target, cli)
    return target


def _install_mac_app(bundle: Path, cli: str) -> None:
    """A minimal .app whose executable is a shell script.

    Nothing heavier is needed: the bundle exists so Spotlight, the Dock, and
    Raycast have something to point at — the actual work is one exec. No
    LSUIElement here (unlike the tray's bundle), because being findable in the
    app switcher is the entire point.
    """
    import plistlib

    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    script = macos / "dispatch-inbox"
    script.write_text(
        "#!/bin/sh\n"
        "# Generated by `dispatch open --shortcut`. Re-run that if the CLI moves.\n"
        f'exec "{cli}" open\n'
    )
    script.chmod(0o755)

    with (bundle / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.dispatch.inbox",
                "CFBundleName": SHORTCUT_NAME,
                "CFBundleDisplayName": SHORTCUT_NAME,
                "CFBundleExecutable": "dispatch-inbox",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "11.0",
            },
            fh,
        )

    # Without an explicit register, Spotlight can take minutes to notice a
    # freshly written bundle — long enough for the user to conclude it failed.
    subprocess.run([_LSREGISTER, "-f", str(bundle)], check=False, capture_output=True)


def _install_windows_lnk(lnk: Path, cli: str) -> None:
    """A Start-menu .lnk, written through the WScript.Shell COM object.

    PowerShell ships with Windows and pywin32 does not, so this stays a
    zero-dependency path. WindowStyle 7 (minimized) keeps the console the
    console-mode entry point opens from flashing in the user's face.
    """
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({lnk});"
        "$s.TargetPath = {cli};"
        "$s.Arguments = 'open';"
        "$s.Description = 'Open the Dispatch inbox';"
        "$s.WindowStyle = 7;"
        "$s.Save()"
    ).format(lnk=_ps_quote(str(lnk)), cli=_ps_quote(cli))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise OSError(
            "could not write the Start menu shortcut: "
            + (proc.stderr.strip() or f"powershell exited {proc.returncode}")
        )


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell literal (doubling embedded quotes)."""
    return "'" + value.replace("'", "''") + "'"


def _install_desktop_entry(path: Path, cli: str) -> None:
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={SHORTCUT_NAME}\n"
        "Comment=Open the Dispatch inbox\n"
        f"Exec={cli} open\n"
        "Terminal=false\n"
        "Categories=Network;Utility;\n"
    )
    path.chmod(0o755)
    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run([updater, str(path.parent)], check=False, capture_output=True)


def shortcut_hint() -> str:
    """How to actually use the thing we just installed, per platform."""
    if sys.platform == "darwin":
        return (f"Press ⌘-Space and type “{SHORTCUT_NAME}”. Drag it to the Dock to "
                "keep it one click away.")
    if sys.platform == "win32":
        return (f"Press Start and type “{SHORTCUT_NAME}”. Right-click it to pin it, "
                "or set a hotkey in the shortcut's Properties.")
    return f"It's in your application launcher as “{SHORTCUT_NAME}”."
