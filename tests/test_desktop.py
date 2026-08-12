"""Tests for opening the desktop UI (``dispatch.desktop``).

Four contracts, all of the quiet-failure kind — each one keeps working, or
appears to, while doing the wrong thing:

  1. **The token belongs in the fragment.** A fragment never leaves the
     machine; a query string does. Moving ``t=`` to ``?t=`` would still render
     the app on the developer's machine (the SPA could be taught to read it)
     while starting to write the loopback token into every access log it
     passes through.

  2. **"Alive" means answered, not 200.** The daemon returning 401/404 proves
     it is up. Tightening this to a 200-only check makes ``dispatch open``
     spawn a *second* daemon on a machine that already has one — two daemons
     fighting over one port, which surfaces much later as dispatches that
     vanish.

  3. **The window must actually be a window.** ``--app`` is the single flag
     separating "desktop app" from "a tab behind 40 other tabs". Nothing else
     in the command line announces its absence.

  4. **The launcher must not squat on the tray's bundle.** The tray owns
     ~/Applications/Dispatch.app; a launcher that took that path would
     overwrite it, and the user would keep launching the wrong one out of
     Spotlight with no error anywhere.

  5. **Loopback must not be proxied, and a shortcut must point at something.**
     Both are Windows-shaped versions of the same failure: the OS quietly
     supplies a default (a registry proxy, a shortcut that saves regardless of
     its target) and the code reads the result as success.
"""
import os
import subprocess
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dispatch import desktop


# ---------------------------------------------------------------------------
# 1. URL shape
# ---------------------------------------------------------------------------


def test_credentials_live_in_the_fragment_never_the_query():
    url = desktop.app_url(8001, "sekrit-token", "d29f-1234")
    path, _, fragment = url.partition("#")
    assert path == "http://127.0.0.1:8001/"
    assert "?" not in url          # nothing is ever sent to the server
    assert "t=sekrit-token" in fragment
    assert "d=d29f-1234" in fragment


def test_dispatch_id_is_optional():
    assert desktop.app_url(8001, "tok") == "http://127.0.0.1:8001/#t=tok"


def test_no_token_yields_a_bare_url_rather_than_an_empty_parameter():
    # A stray "#t=" would make the SPA bootstrap read an empty credential and
    # fail with a 401 instead of falling through to its own sign-in path.
    assert desktop.app_url(8001, "") == "http://127.0.0.1:8001/"


# ---------------------------------------------------------------------------
# 2. Daemon liveness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 404, 500])
def test_an_http_error_still_proves_the_daemon_is_up(monkeypatch, status):
    def raise_http_error(*_a, **_kw):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8001/", status, "nope", {}, None
        )

    monkeypatch.setattr(desktop._DIRECT, "open", raise_http_error)
    assert desktop.daemon_alive(8001) is True


def test_connection_refused_means_down(monkeypatch):
    def refuse(*_a, **_kw):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(desktop._DIRECT, "open", refuse)
    assert desktop.daemon_alive(8001) is False


def test_the_loopback_opener_can_reach_nothing_but_the_url_it_is_given():
    # Passing ProxyHandler({}) suppresses the default ProxyHandler — the one
    # that calls getproxies(), which on Windows reads HKCU Internet Settings —
    # and then build_opener drops the empty replacement too, since a
    # ProxyHandler holding no proxies registers no *_open methods. Nothing is
    # left that could redirect a request at a proxy.
    handlers = desktop._DIRECT.handlers
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)
    assert any(isinstance(h, urllib.request.HTTPHandler) for h in handlers)


def test_daemon_alive_ignores_a_configured_proxy(monkeypatch):
    """A system proxy must not be allowed to answer for 127.0.0.1.

    This is the check whose false *positive* is expensive: `daemon_alive`
    counts any HTTP response, including an error, as proof the daemon is up,
    and a proxy that cannot reach the requested host answers 407 or 502. Route
    the probe through one and `dispatch open` decides a daemon is already
    running, skips starting one, and opens a window onto nothing.
    """
    class Ok(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Port 9 is discard; nothing will ever answer there, so a proxied
        # request fails and daemon_alive would report the daemon down.
        for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        assert desktop.daemon_alive(server.server_port, timeout=5.0) is True
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 3. The window
# ---------------------------------------------------------------------------


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """Capture the argv `open_window` would have launched."""
    calls: list[list[str]] = []
    monkeypatch.setattr(desktop, "_spawn_detached", calls.append)
    monkeypatch.setattr(desktop, "app_profile_dir", lambda: tmp_path / "app-profile")
    return calls


def test_app_mode_and_a_private_profile(monkeypatch, spawned, tmp_path):
    monkeypatch.setattr(desktop, "find_chromium", lambda: tmp_path / "chrome")
    mode, exe = desktop.open_window("http://127.0.0.1:8001/#t=tok")

    assert mode == "app"
    assert exe == tmp_path / "chrome"
    cmd = spawned[0]
    assert cmd[0] == str(tmp_path / "chrome")
    assert "--app=http://127.0.0.1:8001/#t=tok" in cmd
    # The token is in this command line, so it must not be written into the
    # profile the user browses the rest of the web with.
    assert f"--user-data-dir={tmp_path / 'app-profile'}" in cmd


def test_no_chromium_falls_back_to_the_default_browser(monkeypatch, spawned):
    opened: list[str] = []
    monkeypatch.setattr(desktop, "find_chromium", lambda: None)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)

    mode, exe = desktop.open_window("http://127.0.0.1:8001/#t=tok")

    assert (mode, exe) == ("browser", None)
    assert opened == ["http://127.0.0.1:8001/#t=tok"]
    assert spawned == []


def test_browser_flag_skips_chromium_even_when_present(monkeypatch, spawned, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(desktop, "find_chromium", lambda: tmp_path / "chrome")
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)

    mode, _ = desktop.open_window("http://x", prefer_browser=True)

    assert mode == "browser"
    assert opened == ["http://x"]
    assert spawned == []


def test_unset_windows_variables_are_not_mistaken_for_paths(monkeypatch):
    # os.path.expandvars leaves an unset %VAR% verbatim, so on a machine
    # without, say, %PROGRAMFILES(X86)% the candidate stays a literal string.
    # It must never be returned as a browser to exec.
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_CHROMIUM",
                        {"win32": (r"%NO_SUCH_VAR_HERE%\chrome.exe",)})
    monkeypatch.setattr(desktop, "_registered_browsers", list)
    monkeypatch.setattr(desktop.shutil, "which", lambda _n: None)
    assert desktop.find_chromium() is None


# ---------------------------------------------------------------------------
# 3b. Finding a browser on Windows
# ---------------------------------------------------------------------------


class _FakeRegKey:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_winreg(hive: dict):
    """A winreg stand-in. ``hive`` maps (root, access, exe) -> default value."""
    module = types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        HKEY_LOCAL_MACHINE="HKLM",
        KEY_READ=0x20019,
        KEY_WOW64_64KEY=0x0100,
        QueryValueEx=lambda key, _name: (key.value, 1),
    )

    def open_key(root, subkey, _reserved, access):
        exe = subkey.rsplit("\\", 1)[-1]
        if (root, access, exe) not in hive:
            raise FileNotFoundError(subkey)
        return _FakeRegKey(hive[(root, access, exe)])

    module.OpenKey = open_key
    return module


def test_app_paths_is_consulted_because_windows_has_no_browser_on_path(monkeypatch, tmp_path):
    """The PATH fallback finds nothing on Windows; App Paths is the real index.

    ``_CHROMIUM_ON_PATH`` lists Linux binary names, so on Windows every
    ``shutil.which`` returns None and the whole fallback is dead code — a
    machine whose Chrome sits somewhere the fixed table does not name (a
    non-default install directory, an enterprise deployment) reads as having
    no browser at all.
    """
    chrome = tmp_path / "somewhere else" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    brave = tmp_path / "brave.exe"
    brave.write_text("")

    read = _fake_winreg({
        # Quoted values are legal in App Paths and must be unquoted.
        ("HKCU", 0x20019, "chrome.exe"): f'"{chrome}"',
        # Only visible through the 64-bit view of HKLM.
        ("HKLM", 0x20019 | 0x0100, "brave.exe"): str(brave),
    })
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", read)

    assert desktop._registered_browsers() == [chrome, brave]

    # And the ordering survives into discovery: chrome.exe is looked up first.
    monkeypatch.setattr(desktop, "_CHROMIUM", {"win32": ()})
    assert desktop.find_chromium() == chrome


def test_app_paths_is_only_read_on_windows(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "winreg", None)  # would blow up if touched
    assert desktop._registered_browsers() == []


@pytest.mark.skipif(os.name != "nt", reason="%VAR% expansion is ntpath-only")
def test_a_32bit_python_still_finds_64bit_chrome(monkeypatch, tmp_path):
    # WOW64 rewrites %PROGRAMFILES% to the (x86) tree for a 32-bit process, so
    # a table built only from PROGRAMFILES probes the wrong half of the disk
    # and concludes Chrome is not installed. %ProgramW6432% always names the
    # 64-bit tree, whatever bitness is asking.
    wow = tmp_path / "Program Files (x86)"
    native = tmp_path / "Program Files"
    chrome = native / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")

    monkeypatch.setattr(desktop, "_registered_browsers", list)
    monkeypatch.setenv("PROGRAMFILES", str(wow))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(wow))
    monkeypatch.setenv("ProgramW6432", str(native))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert desktop.find_chromium() == chrome


@pytest.mark.skipif(os.name != "nt", reason="reads this machine's real registry")
def test_this_machine_actually_has_a_browser():
    # Windows 10+ always ships Edge, so a None here is a discovery bug rather
    # than a bare machine.
    found = desktop.find_chromium()
    assert found is not None and found.exists()
    assert found.suffix.lower() == ".exe"


def test_the_two_browser_tables_are_one_table():
    # dispatch.browser used to carry its own shorter list (no Brave, no
    # Chromium on Windows) while its error message offered both, so the same
    # machine could open a desktop window and still be told no browser exists.
    from dispatch import browser

    assert browser.chromium_candidates is desktop.chromium_candidates


# ---------------------------------------------------------------------------
# 4. The launcher
# ---------------------------------------------------------------------------


def test_launcher_does_not_collide_with_the_trays_bundle(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    from dispatch.tray.bundle import APP_DIR  # the tray's own ~/Applications/Dispatch.app

    assert desktop.shortcut_path() != APP_DIR
    assert desktop.shortcut_path().parent == APP_DIR.parent


def test_windows_shortcut_lands_in_the_start_menu(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = desktop.shortcut_path()
    assert path.parent == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    assert path.suffix == ".lnk"


def test_powershell_literals_survive_an_apostrophe():
    # C:\Users\O'Brien is a real path shape. Unescaped, it terminates the
    # string mid-command: the shortcut silently isn't created, or worse.
    assert desktop._ps_quote(r"C:\Users\O'Brien\dispatch.exe") == \
        r"'C:\Users\O''Brien\dispatch.exe'"


def test_a_shortcut_is_never_written_to_a_target_that_does_not_exist(monkeypatch, tmp_path):
    # WScript.Shell happily saves a .lnk whose TargetPath names nothing, and
    # powershell exits 0 — so the failure only ever surfaces as a Start-menu
    # entry that does nothing when the user clicks it, months later.
    ran: list = []
    monkeypatch.setattr(desktop, "run_quiet", lambda *a, **k: ran.append(a))

    with pytest.raises(OSError, match="no such file"):
        desktop._install_windows_lnk(tmp_path / "x.lnk", str(tmp_path / "gone.exe"))
    assert ran == [], "powershell must not even be started"


def test_a_shortcut_that_was_not_written_is_reported(monkeypatch, tmp_path):
    cli = tmp_path / "dispatch.exe"
    cli.write_text("")
    monkeypatch.setattr(
        desktop, "run_quiet",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    with pytest.raises(OSError, match="was not created"):
        desktop._install_windows_lnk(tmp_path / "Dispatch Inbox.lnk", str(cli))


def test_the_shortcut_script_stops_on_error_and_reports_it(monkeypatch, tmp_path):
    cli = tmp_path / "dispatch.exe"
    cli.write_text("")
    lnk = tmp_path / "Dispatch Inbox.lnk"
    seen: dict = {}

    def fake_run(cmd, **_kw):
        seen["cmd"] = cmd
        lnk.write_text("")  # what a successful Save() leaves behind
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(desktop, "run_quiet", fake_run)
    desktop._install_windows_lnk(lnk, str(cli))

    exe, script = seen["cmd"][0], seen["cmd"][-1]
    # Resolved absolutely: a Start-menu install is the last place to run
    # whatever an earlier PATH entry calls powershell.exe.
    assert os.path.basename(exe).lower() == "powershell.exe"
    assert "WindowsPowerShell" in exe
    # Without these a COM error inside the pipeline is non-terminating and
    # powershell still exits 0.
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "exit 1" in script


@pytest.mark.skipif(os.name != "nt", reason="runs the real powershell")
def test_a_real_shortcut_round_trips(tmp_path):
    cli = tmp_path / "dispatch.exe"
    cli.write_text("")
    lnk = tmp_path / "Dispatch Inbox.lnk"

    desktop._install_windows_lnk(lnk, str(cli))

    assert lnk.exists() and lnk.stat().st_size > 0
    read_back = desktop.run_quiet([
        desktop._powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
        f"(New-Object -ComObject WScript.Shell).CreateShortcut("
        f"{desktop._ps_quote(str(lnk))}).TargetPath",
    ], timeout=60.0)
    assert read_back.stdout.strip() == str(cli)


@pytest.mark.skipif(os.name != "nt", reason="runs the real powershell")
def test_a_real_powershell_failure_is_not_swallowed(tmp_path):
    cli = tmp_path / "dispatch.exe"
    cli.write_text("")
    # Save() into a directory that does not exist throws; before the try/catch
    # that exception left the exit code at 0.
    with pytest.raises(OSError):
        desktop._install_windows_lnk(tmp_path / "no" / "such" / "dir.lnk", str(cli))


def test_mac_bundle_is_launchable_and_points_at_the_cli(monkeypatch, tmp_path):
    import plistlib

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "cli_executable", lambda: "/opt/bin/dispatch")
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **kw: None)  # lsregister
    monkeypatch.setattr(desktop, "shortcut_path", lambda: tmp_path / "Dispatch Inbox.app")

    bundle = desktop.install_shortcut()

    plist = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    script = bundle / "Contents" / "MacOS" / plist["CFBundleExecutable"]
    assert script.exists(), "CFBundleExecutable must name a file that exists"
    if os.name == "posix":
        # NTFS has no execute bit and Python's Windows chmod cannot set one, so
        # this assertion is only meaningful where the bundle could actually be
        # launched. The rest of the bundle's shape is checked on every platform
        # — a Windows contributor still gets told if they break the plist.
        assert script.stat().st_mode & 0o111, "LaunchServices needs it executable"
    assert '"/opt/bin/dispatch" open' in script.read_text()
    # LSUIElement would hide it from Spotlight and the app switcher — which is
    # the one thing this launcher exists to provide.
    assert not plist.get("LSUIElement")
