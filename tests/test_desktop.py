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
"""
import urllib.error

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

    monkeypatch.setattr(desktop.urllib.request, "urlopen", raise_http_error)
    assert desktop.daemon_alive(8001) is True


def test_connection_refused_means_down(monkeypatch):
    def refuse(*_a, **_kw):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(desktop.urllib.request, "urlopen", refuse)
    assert desktop.daemon_alive(8001) is False


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
    monkeypatch.setattr(desktop.shutil, "which", lambda _n: None)
    assert desktop.find_chromium() is None


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
    assert script.stat().st_mode & 0o111, "LaunchServices needs it executable"
    assert '"/opt/bin/dispatch" open' in script.read_text()
    # LSUIElement would hide it from Spotlight and the app switcher — which is
    # the one thing this launcher exists to provide.
    assert not plist.get("LSUIElement")
