"""Loopback listener helpers with the right socket options per platform.

``SO_REUSEADDR`` does not mean the same thing on Windows as it does on
BSD/macOS, and the difference is a security one rather than a cosmetic one.

On POSIX it means "let me rebind an address stuck in TIME_WAIT" — necessary,
because a restarted daemon must be able to reclaim its own port immediately.
On Windows it means "let me bind even though another socket is *actively
listening* here". Any process on the machine can therefore bind on top of a
running listener and start receiving connections intended for it. For the
daemon's loopback UI — which serves an authenticated local API and hands out
the local token — that turns a stale-port workaround into a local hijack
primitive, and it also breaks the "is this port free?" probe, which answers
"free" for a port that is very much in use.

Windows instead offers ``SO_EXCLUSIVEADDRUSE``, which is the option that
actually expresses the intent. Windows also does not need the POSIX flag: the
TIME_WAIT state applies to connection 4-tuples, not to a closed listening
socket, so a restarting daemon can rebind its port without help.
"""
from __future__ import annotations

import socket
import sys

_IS_WINDOWS = sys.platform == "win32"

# Present on Windows only; referenced through getattr so this module imports
# cleanly everywhere.
_SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)


def apply_reuse_policy(sock: socket.socket) -> None:
    """Set the address-reuse option appropriate to this platform.

    Call before ``bind()``. On Windows this makes the binding exclusive; on
    POSIX it permits reclaiming a TIME_WAIT address.
    """
    if _IS_WINDOWS:
        if _SO_EXCLUSIVEADDRUSE is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                # Setting it can fail if the socket is already bound; the
                # default (no SO_REUSEADDR) is still the safe behaviour.
                pass
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def probe_bindable(host: str, port: int) -> bool:
    """Can we bind ``host:port`` right now?

    Truthful on both platforms: the probe uses the same reuse policy a real
    listener would, so it cannot report a port free that another process is
    already serving.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        apply_reuse_policy(sock)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def listen_socket(host: str, port: int, *, backlog: int = 128) -> socket.socket:
    """A bound, listening TCP socket for ``host:port``.

    Raises OSError if the address is taken — which, with the policy above, now
    means what it says on Windows as well.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        apply_reuse_policy(sock)
        sock.bind((host, port))
        sock.listen(backlog)
    except OSError:
        sock.close()
        raise
    return sock
