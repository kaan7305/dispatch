"""Platform primitives the Windows port rests on.

Everything here guards a *silent* failure — code that kept returning success
on Windows while doing nothing, or doing something unsafe. That is why these
are worth tests even though several of them look like one-liners: none of them
would have raised, so nothing else in the suite could have noticed.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys

import pytest

from dispatch.shared import fsperm, net, proc

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-specific behaviour")


# ---------------------------------------------------------------------------
# Detached spawning
# ---------------------------------------------------------------------------


def test_detach_uses_the_mechanism_this_platform_actually_honours():
    """`start_new_session=True` is accepted and ignored by CPython on Windows.

    That is the whole bug: the call succeeded, so every caller believed it had
    detached a daemon, while the child stayed in the parent's console group and
    died with it.
    """
    kwargs = proc.detached_kwargs()
    if WINDOWS:
        assert "start_new_session" not in kwargs
        flags = kwargs["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs == {"start_new_session": True}


def test_a_detached_child_really_starts_and_is_reported_alive():
    child = proc.spawn_detached(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        assert proc.pid_alive(child.pid) is True
        assert proc.kill_pid(child.pid) is True
        child.wait(timeout=10)
        assert proc.pid_alive(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()


def test_pid_liveness_rejects_a_pid_that_is_not_running():
    assert proc.pid_alive(0) is False
    assert proc.pid_alive(-1) is False
    # A pid this high is not in use; on Windows pids are multiples of 4 and
    # nowhere near this, on Linux it exceeds the default pid_max.
    assert proc.pid_alive(4_000_100) is False


def test_we_never_report_killing_ourselves():
    assert proc.kill_pid(os.getpid()) is False


@windows_only
def test_process_lookup_finds_a_running_image():
    """pids_matching replaced a `pgrep` call that always returned [] here."""
    child = proc.spawn_detached([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        found = proc.pids_matching("python", image=os.path.basename(sys.executable))
        assert child.pid in found
    finally:
        child.kill()
        child.wait(timeout=10)


# ---------------------------------------------------------------------------
# Listener address policy
# ---------------------------------------------------------------------------


def test_a_live_listener_makes_its_port_unbindable():
    """The probe must not call an in-use port free.

    On Windows SO_REUSEADDR means "bind even though someone is listening here",
    not "reclaim a TIME_WAIT address" as it does on BSD. The old probe set it,
    so it answered "free" for a port the daemon was actively serving — and the
    same flag on the real listener let any local process bind on top of the
    loopback UI and receive requests carrying the local bearer token.
    """
    with net.listen_socket("127.0.0.1", 0) as server:
        port = server.getsockname()[1]
        assert net.probe_bindable("127.0.0.1", port) is False


def test_an_unused_port_is_reported_bindable():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert net.probe_bindable("127.0.0.1", port) is True


@windows_only
def test_a_second_listener_cannot_steal_a_bound_port():
    """The hijack itself, stated as a test."""
    with net.listen_socket("127.0.0.1", 0) as server:
        port = server.getsockname()[1]
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with pytest.raises(OSError):
                thief.bind(("127.0.0.1", port))
        finally:
            thief.close()


# ---------------------------------------------------------------------------
# Secret file permissions
# ---------------------------------------------------------------------------


def test_a_secret_is_written_and_readable_by_us(tmp_path):
    target = tmp_path / "nested" / "token.json"
    fsperm.write_private_text(target, '{"token": "sekrit"}')
    assert target.read_text(encoding="utf-8") == '{"token": "sekrit"}'


@windows_only
def test_hardening_replaces_the_inherited_acl_with_one_entry(tmp_path):
    """chmod(0o600) only toggles the read-only attribute on Windows.

    It leaves the inherited profile ACL in place, so the device private key and
    the broker JWT stayed readable by every administrator and by SYSTEM — a
    materially weaker promise than the 0600 the code claimed to be making.
    """
    target = tmp_path / "key"
    fsperm.write_private_text(target, "private-key-material")

    listing = proc.run_quiet(["icacls", str(target)]).stdout
    # One granted identity, and inheritance switched off.
    granted = [
        line for line in listing.splitlines()
        if ":(" in line and "Successfully processed" not in line
    ]
    assert len(granted) == 1, f"expected exactly one ACE, got:\n{listing}"
    assert "NT AUTHORITY\\SYSTEM" not in listing
    assert "BUILTIN\\Administrators" not in listing


@pytest.mark.skipif(WINDOWS, reason="POSIX mode bits")
def test_posix_hardening_is_still_a_0600_chmod(tmp_path):
    target = tmp_path / "key"
    fsperm.write_private_text(target, "x")
    assert target.stat().st_mode & 0o777 == 0o600


@windows_only
def test_the_current_user_sid_resolves():
    sid = fsperm._current_user_sid()
    assert sid.startswith("S-1-")
    assert fsperm._identity() == "*" + sid


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_toast_markup_escapes_what_a_task_title_can_contain():
    """A dispatch title is attacker-adjacent text: it comes from the sender.

    Unescaped, a `<` or `&` in a task title produces malformed XML and the
    toast silently fails to appear — which is exactly the notification telling
    the recipient there is something to approve.
    """
    from dispatch.tray import notify

    xml = notify._toast_xml(
        'Ship & <deploy> "now"', "from a@b.c", "R&D <urgent>", "d-1"
    )
    assert "&amp;" in xml and "&lt;" in xml and "&gt;" in xml
    assert "<deploy>" not in xml
    assert 'launch="dispatch://open?d=d-1"' in xml
    assert 'activationType="protocol"' in xml


def test_a_toast_without_a_dispatch_still_has_a_launch_target():
    from dispatch.tray import notify

    xml = notify._toast_xml("Dispatch", "", "signed out", None)
    assert 'launch="dispatch://open"' in xml


# ---------------------------------------------------------------------------
# The tray
# ---------------------------------------------------------------------------


@windows_only
def test_the_tray_icon_renders_every_state():
    pytest.importorskip("PIL")
    from dispatch.tray.icons import render

    for state in ("ok", "busy", "error"):
        img = render(state, 32)
        assert img.size == (32, 32)
        assert img.mode == "RGBA"
        # Something was actually drawn: getbbox() is None for a fully
        # transparent image, and a blank tray icon is indistinguishable from a
        # missing one at 16px.
        assert img.getbbox() is not None


@windows_only
def test_the_three_states_are_visually_distinct():
    pytest.importorskip("PIL")
    from dispatch.tray.icons import render

    seen = {state: render(state, 16).tobytes() for state in ("ok", "busy", "error")}
    assert len(set(seen.values())) == 3


def test_the_tray_entry_point_routes_by_platform():
    """`dispatch-tray` pointed straight at the rumps app on every platform, so
    on Windows pip installed a launcher whose only possible outcome was an
    ImportError on `import objc`."""
    import dispatch.tray as tray

    assert callable(tray.main)
    # Importing the router must not drag in either platform's UI toolkit.
    assert "rumps" not in sys.modules
