"""The workflow engine's filesystem nodes against Windows path semantics.

A workflow arrives from a remote sender, so every path in it is untrusted
input that the recipient's daemon opens with its own privileges. The
containment check (`relative_to(workspace)`) is the whole of the defence on
macOS and is genuinely sufficient there. On Windows it is not: `NUL`,
`report.` and `a:b.txt` all resolve to something inside the workspace and all
address something other than the file they name.

Two of those are decided by name, and one is not. `test_measured_device_names`
records why: the classic reserved-name table (CON, PRN, AUX, COM1…) does not
match how Windows 11 behaves — most of those are ordinary files now — so the
device case is caught by verifying the write instead of by predicting it.
Keeping the measurement as a test means the day a Windows update changes the
answer, this file says so rather than the guard quietly becoming wrong.

The most important test here is `test_nul_write_is_silently_discarded`, which
does not test our code at all — it pins the platform behaviour that makes the
rest of the file necessary. Unguarded, a `file.write` node targeting NUL
returns `{"bytes_written": 13}` and the workspace stays empty.

Async bodies use asyncio.run(), matching this repo's convention rather than
pytest-asyncio.
"""
import asyncio
import inspect
import sys
from pathlib import Path

import pytest

from dispatch.daemon.workflows import WorkflowEngine, _NodeError
from dispatch.shared import winpath

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows path semantics"
)


def _engine() -> WorkflowEngine:
    # The filesystem and notify nodes touch none of the engine's broker
    # plumbing, so a bare instance is enough to drive them.
    return WorkflowEngine(local_state=None, broker_url="http://broker", broker_token="t")


def _write(engine, workspace, path, content="hello world!!", **params):
    node = {"params": {"path": path, "content": content, **params}}
    return asyncio.run(engine._run_file_write_node(node, {}, {}, workspace))


def _read(engine, workspace, path):
    node = {"params": {"path": path}}
    return asyncio.run(engine._run_file_read_node(node, {}, {}, workspace))


# ── the platform behaviour that makes the guard necessary ──────────────

@_WINDOWS_ONLY
def test_nul_write_is_silently_discarded(tmp_path):
    """Writing to NUL succeeds, reports a byte count, and stores nothing."""
    ws = tmp_path.resolve()
    target = ws / "NUL"
    assert target.write_bytes(b"hello world!!") == 13
    assert target.exists()
    # And it passes the containment check the engine used to rely on alone.
    assert target.resolve().relative_to(ws) == Path("NUL")
    assert list(ws.iterdir()) == []


@_WINDOWS_ONLY
def test_measured_device_names(tmp_path):
    """Which names are actually devices, measured rather than assumed.

    The folklore table says CON, PRN, AUX, NUL, COM1-9 and LPT1-9 are reserved
    in every directory, with the match made on the text before the first dot —
    so `com1.csv` and `nul.txt` would be devices too. On Windows 11 that is
    not what happens, and a guard built from the table rejects a pile of
    perfectly ordinary filenames.

    If this test starts failing, the guard's shape needs revisiting: either
    Windows revived a device name, or it retired the last one.
    """
    ws = tmp_path.resolve()
    ordinary = ["CON", "AUX", "PRN", "COM1", "LPT1",
                "nul.txt", "aux.txt", "con.md", "com1.csv"]
    for name in ordinary:
        target = ws / name
        target.write_bytes(b"payload-1234")
        assert target.read_bytes() == b"payload-1234", f"{name} did not round-trip"
        assert name in [c.name for c in ws.iterdir()], f"{name} was not listed"

    # The one true device, and the reason verify_written exists.
    nul = ws / "NUL"
    assert nul.write_bytes(b"payload-1234") == 12
    assert nul.stat().st_size == 0
    assert "NUL" not in [c.name for c in ws.iterdir()]


# ── the guard: names rejected on sight ─────────────────────────────────

@_WINDOWS_ONLY
@pytest.mark.parametrize(
    "path",
    [
        "report.",                  # Win32 strips the dot; collides with "report"
        "sub/report.",
        "dir /notes.txt",           # trailing space on a directory component
        "notes/report.txt:hidden",  # NTFS alternate data stream
    ],
)
def test_file_write_rejects_names_windows_would_rewrite(tmp_path, path):
    engine = _engine()
    with pytest.raises(_NodeError) as exc:
        _write(engine, tmp_path, path)
    assert "not usable on this platform" in str(exc.value)
    # Nothing was created under any spelling.
    assert list(tmp_path.iterdir()) == []


# ── the guard: writes that did not land ────────────────────────────────

@_WINDOWS_ONLY
def test_file_write_refuses_to_report_success_for_a_discarded_write(tmp_path):
    """The null device accepts the write and reports the byte count.

    Predicting the name is the wrong shape of fix (see
    `test_measured_device_names`); checking that the bytes are in the file
    afterwards catches this one, catches any other name the filesystem
    rewrites, and never rejects a name that works.
    """
    engine = _engine()
    with pytest.raises(_NodeError) as exc:
        _write(engine, tmp_path, "NUL")
    assert "device" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


@_WINDOWS_ONLY
@pytest.mark.parametrize("name", ["CON", "COM1", "nul.txt", "aux.txt", "com1.csv"])
def test_names_that_only_look_reserved_are_written_normally(tmp_path, name):
    """The regression the folklore table would cause.

    `com1.csv` is a plausible export filename and `con.md` a plausible note.
    Rejecting them would break workflows that have every right to run.
    """
    engine = _engine()
    result = _write(engine, tmp_path, name, content="ok")
    assert (tmp_path / name).read_text() == "ok"
    assert result["bytes_written"] == 2


@_WINDOWS_ONLY
def test_file_write_rejects_drive_relative_path(tmp_path):
    """"a:b.txt" is Win32's drive-relative form, so it lands on drive A's
    current directory. The containment check already catches this one — the
    assertion is that it stays caught, by either layer."""
    engine = _engine()
    with pytest.raises(_NodeError):
        _write(engine, tmp_path, "a:b.txt")
    assert list(tmp_path.iterdir()) == []


@_WINDOWS_ONLY
def test_reading_a_missing_file_fails_instead_of_hanging(tmp_path):
    """The defence against a blocking read is that it happens off the loop.

    A `file.read` of a console device would block until something typed at a
    console the daemon does not have — freezing the local UI, the broker
    socket, and every other in-flight dispatch, at the request of one remote
    sender. On this machine `<workspace>/CON` is not a device and simply does
    not exist, so this asserts the ordinary failure; the structural fix is that
    these nodes run their IO in a worker thread (see
    `test_filesystem_nodes_are_coroutines`), which bounds the damage to the one
    node rather than the whole daemon.
    """
    engine = _engine()
    with pytest.raises(_NodeError):
        _read(engine, tmp_path, "CON")


@_WINDOWS_ONLY
def test_overlong_path_names_max_path(tmp_path):
    engine = _engine()
    deep = "/".join(["directory-with-a-fairly-long-name"] * 12) + "/out.txt"
    with pytest.raises(_NodeError) as exc:
        _write(engine, tmp_path, deep)
    assert "MAX_PATH" in str(exc.value)


@_WINDOWS_ONLY
def test_ordinary_paths_still_work(tmp_path):
    engine = _engine()
    result = _write(engine, tmp_path, "sub/notes.txt", content="ok")
    assert (tmp_path / "sub" / "notes.txt").read_text() == "ok"
    assert result["bytes_written"] == 2
    # A name that merely *contains* a device name is a normal file.
    _write(engine, tmp_path, "console.txt", content="ok")
    _write(engine, tmp_path, "my-nul-notes.md", content="ok")


# ── inert off Windows ──────────────────────────────────────────────────

def test_validator_is_inert_off_windows(monkeypatch):
    """macOS must keep treating these as ordinary filenames — `NUL` there is
    a file, and a workflow that writes one works today."""
    monkeypatch.setattr(winpath, "_IS_WINDOWS", False)
    for name in ("NUL", "nul.txt", "report.", "a:b.txt", "CON"):
        winpath.reject_reserved(name)
    winpath.reject_too_long("/" + "a" * 4000)


def test_rules_are_inspectable_from_any_platform():
    """The analysis is platform-independent even though the enforcement is
    not, so the rule table stays testable on the macOS CI box."""
    assert winpath.reserved_reason("report.") is not None
    assert winpath.reserved_reason("a:b.txt") is not None
    assert winpath.reserved_reason("dir/file.txt:stream") is not None
    assert winpath.reserved_reason("notes/report.txt") is None
    # Not decided by name: these are ordinary files on Windows 11, and the
    # device case is caught by verifying the write instead.
    assert winpath.reserved_reason("nul.txt") is None
    assert winpath.reserved_reason("com1.csv") is None
    # COM10 is not a device: only a single digit follows COM/LPT.
    assert winpath.reserved_reason("COM10") is None
    # ".." is navigation, not a name ending in a dot; the containment check
    # owns that case and this rule must not shadow it with a wrong message.
    assert winpath.reserved_reason("a/../b") is None


# ── blocking IO is off the event loop ──────────────────────────────────

@pytest.mark.parametrize(
    "name", ["_run_file_read_node", "_run_file_write_node", "_run_context_node"]
)
def test_filesystem_nodes_are_coroutines(name):
    """These nodes run on the daemon's shared event loop. If one of them ever
    becomes a plain def again, a slow or unresponsive file freezes the local
    UI, the broker WebSocket, and every other in-flight dispatch."""
    assert inspect.iscoroutinefunction(getattr(WorkflowEngine, name))


# ── line endings ───────────────────────────────────────────────────────

def test_written_bytes_are_lf_on_every_platform(tmp_path):
    """A context pack's bytes must hash the same on macOS and Windows."""
    engine = _engine()
    _write(engine, tmp_path, "a.txt", content="one\ntwo\n")
    assert (tmp_path / "a.txt").read_bytes() == b"one\ntwo\n"

    node = {"params": {"files": [{"path": "b.txt", "content": "one\ntwo\n"}]}}
    asyncio.run(engine._run_context_node(node, {}, {}, tmp_path))
    assert (tmp_path / "b.txt").read_bytes() == b"one\ntwo\n"


def test_returned_path_is_json_safe(tmp_path):
    """`str(target)` yields C:\\Users\\... — hydrated into a JSON body template
    by a later http.request node, \\U and \\m are illegal escapes and the run
    dies several nodes downstream as an opaque remote 400."""
    engine = _engine()
    result = _write(engine, tmp_path, "sub/notes.txt", content="ok")
    assert "\\" not in result["path"]
    read_back = _read(engine, tmp_path, "sub/notes.txt")
    assert "\\" not in read_back["path"]

    node = {"params": {"files": [{"path": "c.txt", "content": "x"}]}}
    out = asyncio.run(engine._run_context_node(node, {}, {}, tmp_path))
    assert "\\" not in out["files_written"][0]["path"]


# ── notify.sound ───────────────────────────────────────────────────────

@_WINDOWS_ONLY
@pytest.mark.parametrize("sound", ["Ping", "Basso", "Glass", "Submarine", "default"])
def test_sound_node_never_fails_the_run_on_windows(sound):
    """It used to exec /usr/bin/afplay unconditionally; the FileNotFoundError
    became a _NodeError and a decorative chime failed the whole workflow."""
    engine = _engine()
    result = asyncio.run(engine._run_sound_node({"params": {"sound": sound}}, {}, {}))
    assert result["sound"] == sound
    assert "played" in result


@_WINDOWS_ONLY
def test_sound_node_still_rejects_a_malformed_name():
    """An invalid sound *name* is a bug in the sender's workflow and fails the
    same way on every platform."""
    engine = _engine()
    with pytest.raises(_NodeError):
        asyncio.run(
            engine._run_sound_node({"params": {"sound": "../../etc/passwd"}}, {}, {})
        )
