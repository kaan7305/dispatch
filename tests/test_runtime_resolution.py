"""Tests for agent-runtime detection (``dispatch.executor.runtime``).

The bug these pin down reported success at every observable layer and failed
only where nobody was looking. ``npm i -g @anthropic-ai/claude-code`` on Windows
writes three files into its global bin — ``claude`` (a POSIX shell shim),
``claude.cmd`` and ``claude.ps1`` — and none of them is an image CreateProcess
can start. Detection that asks only "does a file named `claude` exist" answers
yes, ``find_runtime()`` returns a root, ``prepare_agent_runtime()`` logs
success, the installed-runtimes panel goes green, and then every single
dispatch dies at spawn inside the agent session.

So the contracts here are:

  1. **Presence is not executability on Windows.** The npm trio must not
     register; only a real ``.exe``/``.com`` may.
  2. **POSIX is untouched.** An extensionless executable is the normal shape of
     a runtime on macOS and Linux and must keep resolving.
  3. **The unrunnable case has a name.** "installed but unspawnable" is a
     distinct reported state from "not installed", because the remediation
     differs and the user believes they already did the install.
"""
import os
import sys

import pytest

from dispatch.executor import runtime


def _bin(tmp_path, *names):
    """A vendored-runtime root whose bin/ holds exactly `names`."""
    bindir = tmp_path / "vendor" / "bin"
    bindir.mkdir(parents=True)
    for name in names:
        (bindir / name).write_text("", encoding="utf-8")
    return tmp_path / "vendor"


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def as_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


# ---------------------------------------------------------------------------
# _exe: what counts as an executable image
# ---------------------------------------------------------------------------

def test_npm_shim_trio_is_not_executable_on_windows(tmp_path, as_windows):
    """The exact output of `npm i -g @anthropic-ai/claude-code` on Windows."""
    root = _bin(tmp_path, "claude", "claude.cmd", "claude.ps1")
    assert runtime._exe(root / "bin", "claude") is False


@pytest.mark.parametrize("name", ["claude", "claude.cmd", "claude.bat", "claude.ps1"])
def test_no_single_shim_flavour_passes_on_windows(tmp_path, as_windows, name):
    root = _bin(tmp_path, name)
    assert runtime._exe(root / "bin", "claude") is False


def test_real_exe_is_executable_on_windows(tmp_path, as_windows):
    root = _bin(tmp_path, "claude", "claude.cmd", "claude.ps1", "claude.exe")
    assert runtime._exe(root / "bin", "claude") is True


def test_com_image_is_accepted_on_windows(tmp_path, as_windows):
    root = _bin(tmp_path, "node.com")
    assert runtime._exe(root / "bin", "node") is True


def test_posix_extensionless_binary_still_resolves(tmp_path, as_posix):
    """The macOS/Linux shape of a vendored runtime — unchanged behaviour."""
    root = _bin(tmp_path, "claude", "node")
    assert runtime._exe(root / "bin", "claude") is True
    assert runtime._exe(root / "bin", "node") is True


def test_posix_does_not_require_an_extension(tmp_path, as_posix):
    root = _bin(tmp_path, "claude.cmd")
    # A .cmd is meaningless on POSIX, but the POSIX arm was never the problem
    # and we do not tighten it: only `claude` / `claude.exe` are consulted.
    assert runtime._exe(root / "bin", "claude") is False


def test_trailing_dot_does_not_smuggle_a_shim_past_the_check(as_windows):
    # Win32 strips trailing dots and spaces when it opens a file, so these name
    # the same image the suffix test is deciding about.
    assert runtime._is_native_image("claude.exe.") is True
    assert runtime._is_native_image("claude.cmd.") is False
    assert runtime._is_native_image(r"C:\tools\claude.exe") is True
    assert runtime._is_native_image("/usr/local/bin/claude") is False


# ---------------------------------------------------------------------------
# _native_exe: PATH lookups that only count runnable hits
# ---------------------------------------------------------------------------

def _fake_which(monkeypatch, table):
    monkeypatch.setattr(runtime.shutil, "which", lambda name: table.get(name))


def test_native_exe_rejects_a_cmd_shim(monkeypatch, as_windows):
    _fake_which(monkeypatch, {"claude": r"C:\npm\claude.cmd"})
    assert runtime._native_exe("claude") is None
    assert runtime._shim_only("claude") is True


def test_native_exe_rejects_an_extensionless_wrapper(monkeypatch, as_windows):
    _fake_which(monkeypatch, {"claude": r"C:\Users\me\.local\bin\claude"})
    assert runtime._native_exe("claude") is None


def test_native_exe_sees_past_a_shadowing_shim(monkeypatch, as_windows):
    """which() walks PATH directory-major, so an early claude.cmd hides a real
    claude.exe in a later directory. Probing the .exe name finds it."""
    _fake_which(monkeypatch, {
        "claude": r"C:\npm\claude.cmd",
        "claude.exe": r"C:\Program Files\claude\claude.exe",
    })
    assert runtime._native_exe("claude") == r"C:\Program Files\claude\claude.exe"
    assert runtime._shim_only("claude") is False


def test_native_exe_validates_the_exe_probe_too(monkeypatch, as_windows):
    """PATHEXT resolution can append an extension of its own to the probe."""
    _fake_which(monkeypatch, {
        "claude": r"C:\npm\claude.cmd",
        "claude.exe": r"C:\npm\claude.exe.cmd",
    })
    assert runtime._native_exe("claude") is None


def test_native_exe_is_a_plain_which_on_posix(monkeypatch, as_posix):
    _fake_which(monkeypatch, {"claude": "/usr/local/bin/claude"})
    assert runtime._native_exe("claude") == "/usr/local/bin/claude"
    assert runtime._shim_only("claude") is False


# ---------------------------------------------------------------------------
# find_runtime: the gate the false positive got through
# ---------------------------------------------------------------------------

def test_find_runtime_rejects_a_shim_only_root(tmp_path, monkeypatch, as_windows):
    root = _bin(tmp_path, "claude", "claude.cmd", "claude.ps1", "node.cmd")
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [root])
    assert runtime.find_runtime() is None


def test_find_runtime_accepts_claude_exe_without_node(tmp_path, monkeypatch, as_windows):
    """The Windows-native CLI is self-contained; requiring a sibling node
    rejected a root that works."""
    root = _bin(tmp_path, "claude.exe")
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [root])
    assert runtime.find_runtime() == root


def test_find_runtime_prefers_a_root_that_also_has_node(tmp_path, monkeypatch, as_posix):
    partial = _bin(tmp_path / "a", "claude")
    complete = _bin(tmp_path / "b", "claude", "node")
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [partial, complete])
    assert runtime.find_runtime() == complete


def test_find_runtime_ignores_a_node_only_root(tmp_path, monkeypatch, as_posix):
    root = _bin(tmp_path, "node")
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [root])
    assert runtime.find_runtime() is None


# ---------------------------------------------------------------------------
# runtime_status: saying "shim_only" out loud
# ---------------------------------------------------------------------------

def test_status_names_the_shim_instead_of_ticking_it(monkeypatch, as_windows):
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [])
    _fake_which(monkeypatch, {"claude": r"C:\npm\claude.cmd"})

    status = runtime.runtime_status()
    assert status["claude"] is False
    assert status["states"]["claude"] == runtime.STATE_SHIM_ONLY
    assert status["shims"] == ["claude"]
    assert any("claude.cmd" in w for w in status["warnings"])


def test_status_distinguishes_shim_only_from_missing(monkeypatch, as_windows):
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [])
    _fake_which(monkeypatch, {"claude": r"C:\npm\claude.cmd"})
    assert runtime.runtime_status()["states"]["codex"] == runtime.STATE_MISSING


def test_status_reports_node_separately_from_claude(tmp_path, monkeypatch, as_windows):
    root = _bin(tmp_path, "claude.exe")
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [root])
    _fake_which(monkeypatch, {})

    status = runtime.runtime_status()
    assert status["bundled"] is True
    assert status["claude"] is True
    assert status["node"] is False
    assert status["states"]["node"] == runtime.STATE_MISSING


def test_status_marks_an_ambient_native_install_as_path(monkeypatch, as_windows):
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [])
    _fake_which(monkeypatch, {"claude": r"C:\Users\me\.local\bin\claude.exe"})
    assert runtime.runtime_status()["states"]["claude"] == runtime.STATE_PATH


# ---------------------------------------------------------------------------
# Per-user install location
# ---------------------------------------------------------------------------

def test_app_support_vendor_uses_localappdata_on_windows(monkeypatch, as_windows):
    monkeypatch.setenv("LOCALAPPDATA", r"D:\profile\AppData\Local")
    assert runtime._app_support_vendor().parts[-2:] == ("Dispatch", "vendor")
    assert str(runtime._app_support_vendor()).startswith(r"D:\profile\AppData\Local")


def test_app_support_vendor_honours_xdg_on_linux(monkeypatch, as_posix):
    monkeypatch.setenv("XDG_DATA_HOME", "/opt/share")
    assert runtime._app_support_vendor().as_posix() == "/opt/share/Dispatch/vendor"


def test_app_support_vendor_keeps_the_mac_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert runtime._app_support_vendor().parts[-4:] == (
        "Library", "Application Support", "Dispatch", "vendor",
    )


# ---------------------------------------------------------------------------
# PATH wiring
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="case/separator folding is win32")
def test_path_entry_already_present_in_another_spelling_is_not_duplicated(
    tmp_path, monkeypatch
):
    root = _bin(tmp_path, "claude.exe")
    bindir = str((root / "bin").resolve())
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [root])
    monkeypatch.setattr(runtime, "_prepared", False)
    monkeypatch.setenv("DISPATCH_AGENT_RUNTIME", "")
    monkeypatch.setenv("PATH", bindir.upper().replace("\\", "/"))

    assert runtime.prepare_agent_runtime() is True
    assert os.environ["PATH"].count(os.pathsep) == 0


def test_path_gets_the_bindir_when_genuinely_absent(tmp_path, monkeypatch, as_windows):
    root = _bin(tmp_path, "claude.exe")
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [root])
    monkeypatch.setattr(runtime, "_prepared", False)
    monkeypatch.setenv("DISPATCH_AGENT_RUNTIME", "")
    monkeypatch.setenv("PATH", "")

    assert runtime.prepare_agent_runtime() is True
    first = os.environ["PATH"].split(os.pathsep)[0]
    assert os.path.normcase(first) == os.path.normcase(str((root / "bin").resolve()))


def test_prepare_warns_specifically_about_a_shim(tmp_path, monkeypatch, caplog, as_windows):
    monkeypatch.setattr(runtime, "_candidate_roots", lambda: [])
    monkeypatch.setattr(runtime, "_prepared", False)
    monkeypatch.setenv("DISPATCH_AGENT_RUNTIME", "")
    _fake_which(monkeypatch, {"claude": r"C:\npm\claude.cmd"})

    with caplog.at_level("WARNING"):
        assert runtime.prepare_agent_runtime() is False
    assert "not an executable image" in caplog.text


# ---------------------------------------------------------------------------
# The browser capability note (executor) — the same "promised, not present"
# failure mode, in the system prompt instead of the status panel.
# ---------------------------------------------------------------------------

def test_browser_note_prefers_the_installed_launcher(monkeypatch):
    from dispatch.executor import executor

    monkeypatch.setattr(
        executor.shutil, "which",
        lambda name: r"C:\bin\dispatch-browser.exe" if name == "dispatch-browser" else None,
    )
    note = executor._browser_capability_note()
    assert r'"C:\bin\dispatch-browser.exe" open <url>' in note
    assert "python -m dispatch.browser" not in note


def test_browser_note_falls_back_to_this_interpreter(monkeypatch):
    """Never bare `python`: on stock Windows that is the Store alias, which
    opens the Store and exits 9009."""
    from dispatch.executor import executor

    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    monkeypatch.setattr(executor.sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(executor.sys, "frozen", False, raising=False)

    note = executor._browser_capability_note()
    assert r'"C:\venv\Scripts\python.exe" -m dispatch.browser' in note
    assert "`python -m" not in note


def test_browser_note_is_omitted_when_nothing_can_run_it(monkeypatch):
    from dispatch.executor import executor

    monkeypatch.setattr(executor.shutil, "which", lambda name: None)
    monkeypatch.setattr(executor.sys, "frozen", True, raising=False)
    assert executor._browser_capability_note() == ""


# ---------------------------------------------------------------------------
# codex: the same check, feeding `dispatch doctor`
# ---------------------------------------------------------------------------

def test_codex_shim_is_not_reported_as_installed(monkeypatch, tmp_path, as_windows):
    from dispatch import codex

    _fake_which(monkeypatch, {"codex": r"C:\npm\codex.cmd"})
    monkeypatch.setattr(codex, "config_path", lambda: tmp_path / "absent.toml")
    assert codex.codex_on_path() is False
    assert codex.codex_installed() is False


def test_codex_status_flags_the_shim(monkeypatch, tmp_path, as_windows):
    from dispatch import codex

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _fake_which(monkeypatch, {"codex": r"C:\npm\codex.cmd"})

    status = codex.status()
    assert status["codex_on_path"] is False
    assert status["codex_shim_only"] is True
