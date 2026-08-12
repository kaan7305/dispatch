"""Open the local desktop UI on demand, without going through the menu bar.

The "desktop app" is not a separate binary. The daemon serves a single-page
app on the loopback interface and the tray simply points a WKWebView at

    http://127.0.0.1:<local_port>/#t=<local token>[&d=<dispatch id>]

which means the menu bar is an accident of how the UI shipped, not a
requirement of it. That accident bites in two places: a full menu bar has no
room for another icon (macOS silently drops the ones that don't fit), and
Windows has no tray app at all — so on that platform the UI currently has no
door in front of it.

Everything here is therefore platform-neutral apart from two questions: where
the Chromium-family browsers live (``_CHROMIUM``, plus the App Paths registry
on Windows and PATH on Linux — see ``chromium_candidates``, which
``dispatch.browser`` shares) and what counts as a launcher the OS will surface
(``install_shortcut``). The window is an "app mode" browser
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
from dispatch.shared.proc import run_quiet, spawn_detached

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

# Every loopback call in this module goes through an opener with proxies
# explicitly disabled. ``urlopen`` uses ``getproxies()``, which on Windows falls
# back to reading HKCU Internet Settings — so on a machine with a system proxy
# (the norm on managed hardware) a request to 127.0.0.1 is handed to the proxy
# instead. That is worse than a plain failure here: ``daemon_alive`` treats an
# HTTPError as proof of life, and a proxy answering 407 is an HTTPError, so the
# check would report a running daemon on a machine with none and `dispatch open`
# would show a connection-error window rather than starting one.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
        _DIRECT.open(f"http://127.0.0.1:{port}/", timeout=timeout)
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
#
# %ProgramW6432% sits beside every %PROGRAMFILES% because WOW64 rewrites
# PROGRAMFILES to "Program Files (x86)" for a 32-bit process. Under a 32-bit
# Python on 64-bit Windows the 64-bit install — which is what Chrome and Brave
# ship today — would otherwise never be probed at all. ProgramW6432 always
# names the 64-bit tree and is simply unset on 32-bit Windows, where the
# candidate is dropped as an unexpanded variable.
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
        r"%ProgramW6432%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
        # Edge really does install into the (x86) tree even on 64-bit Windows.
        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramW6432%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramW6432%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
        r"%ProgramW6432%\Chromium\Application\chrome.exe",
        r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
    ),
}

# Linux ships browsers on PATH rather than at fixed paths.
_CHROMIUM_ON_PATH = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
)

# ...but a PATH-less environment (a systemd unit, cron) still finds them here.
_CHROMIUM_UNIX_FALLBACK = (
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/snap/bin/chromium",
)

# Windows has no PATH convention for browsers; the registration that actually
# means something is App Paths, which is what ShellExecute("chrome.exe") and
# Start > Run resolve through. Every Chromium-family installer writes it.
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
_APP_PATHS_EXES = (
    "chrome.exe", "msedge.exe", "brave.exe", "chromium.exe",
    "vivaldi.exe", "opera.exe",
)


def _registered_browsers() -> list[Path]:
    """Chromium-family browsers registered under App Paths, preferred first.

    Per-user installs land in HKCU and machine-wide ones in HKLM, and a 32-bit
    process reading HKLM is redirected into Wow6432Node — where a 64-bit
    installer wrote nothing — so the 64-bit view is read explicitly as well.
    """
    if sys.platform != "win32":
        return []
    import winreg

    views = (
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, 0),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    )
    found: list[Path] = []
    for exe in _APP_PATHS_EXES:
        for root, view in views:
            try:
                with winreg.OpenKey(
                    root, rf"{_APP_PATHS_KEY}\{exe}", 0, winreg.KEY_READ | view
                ) as key:
                    # The default value is the full path to the executable.
                    value, _kind = winreg.QueryValueEx(key, "")
            except OSError:
                # Missing key/value raises FileNotFoundError; a hive we are not
                # allowed to read raises the general form. Neither is fatal —
                # this whole lookup is one of several ways to find a browser.
                continue
            text = os.path.expandvars(str(value).strip().strip('"'))
            if text:
                found.append(Path(text))
    return found


def chromium_candidates() -> list[Path]:
    """Every place a Chromium-family browser might be, best guess first.

    Shared with ``dispatch.browser`` so the desktop window and the CDP
    automation agree about what is installed: they used to keep separate
    tables that had drifted apart, and the disagreement showed up as
    "no browser found" from one command on a machine where the other had
    just opened one.
    """
    out: list[Path] = []
    for raw in _CHROMIUM.get(sys.platform, ()):
        expanded = os.path.expandvars(os.path.expanduser(raw))
        # expandvars leaves an unset %VAR% verbatim, and the leftover is not
        # inert: r"%LOCALAPPDATA%\Google\Chrome\..." keeps a leading backslash
        # once the variable is stripped by Path, so it resolves against the
        # drive root. Drop the candidate rather than probe a bogus absolute.
        if "%" in expanded:
            continue
        out.append(Path(expanded))

    if sys.platform == "win32":
        out.extend(_registered_browsers())
    else:
        out.extend(Path(p) for p in map(shutil.which, _CHROMIUM_ON_PATH) if p)
        out.extend(Path(p) for p in _CHROMIUM_UNIX_FALLBACK)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        # The registry repeats most of the fixed table; casefold because
        # Windows paths differ only in spelling, not in identity.
        key = str(path).casefold() if sys.platform == "win32" else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_chromium() -> Path | None:
    """First Chromium-family browser on this machine, or None.

    Only Chromium implements ``--app``; Safari and Firefox have no equivalent,
    which is why the fallback is a plain tab rather than a worse window.
    """
    for path in chromium_candidates():
        if path.exists():
            return path
    return None


def _spawn_detached(cmd: list[str]) -> None:
    """Start a GUI process that outlives this CLI invocation.

    Closing the terminal must not take the window with it, so the child gets
    its own session (POSIX) / process group (Windows).
    """
    spawn_detached(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def _powershell_exe() -> str:
    """Windows PowerShell, by absolute path.

    Not "powershell" off PATH: a Start-menu install is exactly the moment you
    do not want to run whatever a directory earlier in PATH happens to call
    powershell.exe, and a PATH inherited from a service can be missing
    System32 entirely.
    """
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _install_windows_lnk(lnk: Path, cli: str) -> None:
    """A Start-menu .lnk, written through the WScript.Shell COM object.

    PowerShell ships with Windows and pywin32 does not, so this stays a
    zero-dependency path. WindowStyle 7 (minimized) keeps the console the
    console-mode entry point opens from flashing in the user's face.

    Three checks around the same silent failure: CreateShortcut accepts a
    TargetPath that does not exist, Save() writes the .lnk anyway, and
    powershell exits 0. The user got a Start-menu entry that does nothing when
    clicked, with no error at install time to connect it to. So the target is
    validated first, the script is made to fail on its own errors rather than
    only on a crash, and the file is confirmed on disk afterwards.
    """
    if not Path(cli).exists():
        raise OSError(
            f"refusing to write a Start menu shortcut pointing at {cli!r}: "
            "no such file. Re-run `dispatch open --shortcut` from an "
            "environment where the dispatch command is installed."
        )

    ps = (
        "$ErrorActionPreference = 'Stop';"
        "try {{"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({lnk});"
        "$s.TargetPath = {cli};"
        "$s.Arguments = 'open';"
        "$s.Description = 'Open the Dispatch inbox';"
        "$s.WindowStyle = 7;"
        "$s.Save()"
        "}} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}"
    ).format(lnk=_ps_quote(str(lnk)), cli=_ps_quote(cli))

    try:
        result = run_quiet(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not run powershell to write the shortcut: {exc}") from exc

    if result.returncode != 0:
        raise OSError(
            "could not write the Start menu shortcut: "
            + ((result.stderr or "").strip() or f"powershell exited {result.returncode}")
        )
    if not lnk.exists():
        raise OSError(
            f"powershell reported success but {lnk} was not created — the Start "
            "menu folder may be redirected or policy-locked."
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
