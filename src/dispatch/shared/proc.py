"""Cross-platform process helpers: detached spawn + process lookup.

Two things the codebase kept re-deriving, each getting Windows subtly wrong.

**Detached spawn.** Several places start a daemon or tray that must outlive the
process starting it (the CLI, the in-session MCP, the desktop launcher). They
all passed ``start_new_session=True``, which reads like a cross-platform
"detach" but is POSIX-only: CPython's Windows ``_execute_child`` takes the
argument and discards it (it is literally named ``unused_start_new_session``).
The child therefore stayed in the parent's console process group, so closing
the terminal — or the MCP host exiting — killed the daemon it had just spawned.
Windows needs ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` instead.

**Process lookup.** ``pgrep`` does not exist on Windows, so every
``shutil.which("pgrep")`` guard returned no-match unconditionally. Callers read
that as "no daemon/tray is running" and went on to print advice that was flatly
untrue, or to spawn a duplicate.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Optional, Sequence, Union

_Sink = Union[int, IO, None]


def detached_kwargs() -> dict:
    """Popen kwargs that put the child outside this process's lifetime.

    POSIX: its own session (``setsid``), so no controlling terminal and no
    SIGHUP when the terminal closes.
    Windows: ``DETACHED_PROCESS`` (no inherited console — a console-subsystem
    child would otherwise pop a window and die with ours) plus
    ``CREATE_NEW_PROCESS_GROUP`` (so a Ctrl-C in our console is not broadcast
    to it).
    """
    if sys.platform == "win32":
        return {
            "creationflags": (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        }
    return {"start_new_session": True}


def spawn_detached(
    cmd: Sequence[str],
    *,
    stdout: _Sink = subprocess.DEVNULL,
    stderr: _Sink = None,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[dict] = None,
) -> subprocess.Popen:
    """Start ``cmd`` so it survives this process exiting.

    ``stderr`` defaults to following ``stdout`` — callers that pass a log file
    want both streams in it, and that is easy to get wrong in the other
    direction (a dropped stderr is exactly the output you need when the child
    fails to start).
    """
    return subprocess.Popen(
        list(cmd),
        stdout=stdout,
        stderr=stdout if stderr is None else stderr,
        stdin=subprocess.DEVNULL,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        close_fds=True,
        **detached_kwargs(),
    )


def _no_window_kwargs() -> dict:
    """Keep a short-lived helper process from flashing a console window.

    Only meaningful on Windows; CREATE_NO_WINDOW is what stops ``tasklist``
    from blinking a black box on screen when the tray shells out to it.
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def run_quiet(cmd: Sequence[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess:
    """Run a short helper command, capturing output and showing no window."""
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        **_no_window_kwargs(),
    )


# The only two extensions CreateProcess starts as an image. Everything else on
# PATHEXT (.cmd, .bat, .ps1) is a script that needs an interpreter, and an
# extensionless file is nothing at all to Windows.
NATIVE_SUFFIXES = (".exe", ".com")


def is_native_image(path: Union[str, Path]) -> bool:
    """Can this path be handed to CreateProcess directly? (win32 question only)

    Trailing dots and spaces are stripped first because Win32 strips them when
    it opens the file, so ``claude.exe.`` names the same image as
    ``claude.exe`` while a naive suffix test would disagree.
    """
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name.rstrip(". ").lower().endswith(NATIVE_SUFFIXES)


def native_exe(name: str) -> Optional[str]:
    """``shutil.which(name)``, narrowed to a hit the OS can actually execute.

    On win32 ``which`` honours PATHEXT and walks PATH directory-major, so a
    ``claude.cmd`` sitting in an early entry both satisfies the lookup and
    shadows a real ``claude.exe`` further down. Probe each native extension
    explicitly to see past that, and validate the resolved suffix either way —
    PATHEXT resolution can append an extension of its own and hand back
    ``claude.exe.cmd``.

    This lives here rather than next to the runtime resolution that motivated
    it so that callers can ask the question without importing
    ``dispatch.executor``, whose package ``__init__`` pulls in the Agent SDK —
    about 1.2s and 20-odd modules. ``dispatch doctor`` asks it twice.
    """
    import shutil

    hit = shutil.which(name)
    if sys.platform != "win32":
        return hit
    if hit and is_native_image(hit):
        return hit
    for suffix in NATIVE_SUFFIXES:
        probe = shutil.which(f"{name}{suffix}")
        if probe and is_native_image(probe):
            return probe
    return None


def open_external(target: str) -> None:
    """Hand a URL or file to whatever the OS thinks should handle it.

    ``open`` on macOS, ``xdg-open`` on Linux, ShellExecute on Windows. The
    Windows arm uses ``os.startfile`` rather than ``cmd /c start`` on purpose:
    ``start`` is a cmd builtin, so the target would be re-parsed by the shell
    and any ``&`` in a URL query string would split the command.

    Raises OSError if nothing could open it — callers surface that to the user.
    """
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # win32-only
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [opener, target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def pids_matching(pattern: str, *, image: str = "") -> list[int]:
    """PIDs of running processes matching ``pattern``, excluding ourselves.

    POSIX matches the full command line (``pgrep -f``). Windows has no
    equivalent of ``-f`` in the always-present tooling, so callers pass
    ``image`` — the executable name to match with ``tasklist``'s image filter
    (console scripts installed by pip become real ``.exe`` images, so
    ``dispatch-daemon.exe`` is a reliable handle).

    Returns [] rather than raising when the platform tooling is missing: every
    caller treats this as advisory.
    """
    me = os.getpid()
    try:
        if sys.platform == "win32":
            name = image or pattern
            if not name.lower().endswith(".exe"):
                name += ".exe"
            result = run_quiet(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                timeout=5.0,
            )
            pids: list[int] = []
            for row in result.stdout.splitlines():
                # CSV: "image","pid","session","session#","mem"
                fields = [f.strip('" ') for f in row.split('","')]
                if len(fields) < 2:
                    continue
                try:
                    pid = int(fields[1])
                except ValueError:
                    continue  # the "no tasks are running" banner
                if pid != me:
                    pids.append(pid)
            return pids

        import shutil

        pgrep = shutil.which("pgrep")
        if not pgrep:
            return []
        result = run_quiet([pgrep, "-f", pattern], timeout=5.0)
        return [
            int(line)
            for line in result.stdout.split()
            if line.isdigit() and int(line) != me
        ]
    except (OSError, subprocess.SubprocessError):
        return []


def pid_alive(pid: int) -> bool:
    """Is a process with this pid currently running?

    Used to decide whether an advisory record naming a pid can be trusted. On
    Windows a pid can be recycled, so this is a liveness hint, not proof of
    identity — callers pair it with a lock they can actually contend for.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def kill_pid(pid: int) -> bool:
    """Forcibly stop a process. True if we believe it is gone.

    Windows has no SIGKILL; ``TerminateProcess`` is the equivalent and is what
    ``taskkill /F`` calls underneath.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
            if not handle:
                return not pid_alive(pid)
            try:
                return bool(kernel32.TerminateProcess(handle, 1))
            finally:
                kernel32.CloseHandle(handle)
        import signal as _signal

        os.kill(pid, _signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False
