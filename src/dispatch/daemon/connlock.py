"""Machine-level broker-connection ownership lock.

Exactly one process per machine may hold the broker WebSocket. Both the
daemon and the in-session MCP try to acquire this lock before connecting;
whoever holds it is the *connection owner*, and any others stand by. The
lock is an advisory ``flock`` on a file under ``~/.dispatch/``, so the
kernel releases it automatically when the owning process dies — a standby
process then takes over on its next poll. That gives free failover with no
eviction war: ownership is serialized by the lock, never stolen.

A sidecar ``connection.json`` records the owner's pid / role / local UI port
so other processes can discover the owner (used once the MCP becomes a thin
client of the daemon's local API — task 2). It is advisory only; the lock,
not the json, is the source of truth for who owns the connection.

Windows has no ``flock``, and the original fallback here — "no fcntl, so
declare yourself the owner" — quietly inverted the module's whole guarantee:
every process that asked became an owner, so two daemons would each open a
broker WebSocket and the machine would receive and execute dispatches twice.
The Windows path therefore uses ``msvcrt.locking``, which takes a *mandatory*
byte-range lock the kernel releases when the owning process dies. That gives
the same free-failover property as flock: ownership is serialized, never
stolen, and a crash never wedges the next launch.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dispatch.connlock")

# How often a standby process re-tries to acquire ownership. The owner keeps
# the lock for its whole lifetime (across broker reconnects), so a standby
# only ever acquires when the previous owner exits — this poll just bounds
# the failover gap.
STANDBY_POLL_S = 3.0

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt

    fcntl = None  # type: ignore[assignment]
    _HAVE_LOCKING = True
else:
    msvcrt = None  # type: ignore[assignment]
    try:
        import fcntl

        _HAVE_LOCKING = True
    except ImportError:  # a Unix without flock: degrade to "always owner"
        _HAVE_LOCKING = False

# Back-compat alias: tests and older callers probe this to decide whether a
# real lock is available on this platform.
_HAVE_FCNTL = _HAVE_LOCKING


class ConnectionLock:
    """Advisory single-owner lock for the machine's broker connection."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.meta_path = self.path.with_suffix(".json")
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """Try to become the connection owner. Non-blocking; returns whether
        ownership was obtained. Idempotent: returns True if already held."""
        if self._fd is not None:
            return True
        if not _HAVE_LOCKING:
            self._fd = -1  # sentinel: "held" on platforms without any lock
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            if _IS_WINDOWS:
                # Lock one byte at offset 0. msvcrt.locking is mandatory and
                # process-scoped: the kernel drops it when this process exits,
                # however it exits. The byte need not exist — locking a region
                # past EOF is legal and is what makes this work on a fresh file.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    @property
    def held(self) -> bool:
        return self._fd is not None

    def write_owner(self, *, role: str, local_port: Optional[int] = None) -> None:
        """Record who currently owns the connection (advisory, for discovery)."""
        from dispatch.shared import fsperm

        try:
            self.meta_path.write_text(
                json.dumps({"pid": os.getpid(), "role": role, "local_port": local_port}),
                encoding="utf-8",
            )
            fsperm.harden_file(self.meta_path)
        except OSError:
            pass

    def read_owner(self) -> dict:
        """The recorded owner metadata, or {} if none / stale / unreadable.

        The record is dropped when it names a pid that is no longer running.
        On POSIX a stale sidecar is largely harmless — ``release()`` clears it
        and the flock is the real answer — but on Windows an owner killed
        without unwinding leaves the file behind, and a caller that trusted it
        would try to reach a local UI port nothing is serving.
        """
        from dispatch.shared.proc import pid_alive

        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        pid = data.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid):
            return {}
        return data

    def release(self) -> None:
        """Release ownership. Safe to call when not held."""
        if self._fd is None:
            return
        if _HAVE_LOCKING and self._fd >= 0:
            try:
                if _IS_WINDOWS:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None
        # Best-effort: clear the sidecar if it still names us.
        try:
            if self.read_owner().get("pid") == os.getpid():
                self.meta_path.unlink(missing_ok=True)
        except OSError:
            pass
