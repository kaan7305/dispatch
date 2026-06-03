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
client of the daemon's local API — task 2). It is advisory only; the flock,
not the json, is the source of truth for who owns the connection.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dispatch.connlock")

# How often a standby process re-tries to acquire ownership. The owner keeps
# the lock for its whole lifetime (across broker reconnects), so a standby
# only ever acquires when the previous owner exits — this poll just bounds
# the failover gap.
STANDBY_POLL_S = 3.0

try:
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # non-Unix (e.g. Windows): degrade to a no-op "always owner"
    _HAVE_FCNTL = False


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
        if not _HAVE_FCNTL:
            self._fd = -1  # sentinel: "held" on platforms without flock
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
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
        try:
            self.meta_path.write_text(
                json.dumps({"pid": os.getpid(), "role": role, "local_port": local_port})
            )
            self.meta_path.chmod(0o600)
        except OSError:
            pass

    def read_owner(self) -> dict:
        """The recorded owner metadata, or {} if none / unreadable."""
        try:
            return json.loads(self.meta_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def release(self) -> None:
        """Release ownership. Safe to call when not held."""
        if self._fd is None:
            return
        if _HAVE_FCNTL and self._fd >= 0:
            try:
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
