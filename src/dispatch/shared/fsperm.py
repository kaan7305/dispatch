"""Make a file readable only by the user who owns it — on every platform.

``~/.dispatch`` holds broker bearer JWTs, the Anthropic API key, and (with the
file key backend) the Ed25519 device private key. The codebase protected all of
these with ``path.chmod(0o600)`` and a comment explaining that a bearer token
lives there.

On Windows that call does essentially nothing. ``os.chmod`` maps only to the
FILE_ATTRIBUTE_READONLY bit, and since ``0o600`` has the owner-write bit set,
the call clears the read-only flag and discards the rest — the file keeps
whatever ACL it inherited from the user profile. That is not as catastrophic as
world-readable (the profile ACL already excludes other standard users), but it
does leave the key readable by every administrator and by anything running as
SYSTEM, which is a materially weaker promise than the one macOS gets.

So on Windows we set a real DACL: drop inherited ACEs and grant full control to
exactly one identity, the current user. That is the closest equivalent to 0600.

Everything here is best-effort. A failure to harden must never stop the daemon
from running — it degrades to the inherited ACL, which is what we had before.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("dispatch.fsperm")

# Paths already hardened in this process. Re-applying a DACL means spawning
# icacls again, and these files get rewritten on every token refresh.
# Truncating a file (what write_text does) preserves its DACL, so once is
# enough for the lifetime of the file.
_done: set[str] = set()


def _current_user_sid() -> str:
    """The current process's user SID in string form (S-1-5-21-...).

    Preferred over a username because it sidesteps domain-qualification and
    localisation: on a non-English Windows the *names* differ, the SIDs do not.
    """
    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TokenUser = 1

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    # Declaring these is not optional on 64-bit: without an explicit restype a
    # HANDLE comes back through a C int and the top 32 bits are lost, so the
    # GetCurrentProcess pseudo-handle (-1) arrives as 0x00000000FFFFFFFF and
    # OpenProcessToken fails with ERROR_INVALID_HANDLE.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError(f"OpenProcessToken failed ({ctypes.get_last_error()})")
    try:
        size = wintypes.DWORD()
        # First call is expected to fail with ERROR_INSUFFICIENT_BUFFER; its
        # job is to report how many bytes the TOKEN_USER needs.
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TokenUser, buf, size, ctypes.byref(size)
        ):
            raise OSError(f"GetTokenInformation failed ({ctypes.get_last_error()})")
        # TOKEN_USER begins with a SID_AND_ATTRIBUTES whose first member is a
        # PSID pointing into this same buffer.
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
        out = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
            raise OSError(f"ConvertSidToStringSidW failed ({ctypes.get_last_error()})")
        try:
            return str(out.value)
        finally:
            kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _identity() -> str:
    """An icacls principal for the current user: a SID if we can get one,
    otherwise the account name."""
    try:
        return "*" + _current_user_sid()
    except (OSError, AttributeError, ValueError):
        user = os.environ.get("USERNAME") or ""
        domain = os.environ.get("USERDOMAIN") or ""
        return f"{domain}\\{user}" if domain and user else (user or "%USERNAME%")


def _icacls(path: Path, *, container: bool) -> bool:
    from dispatch.shared.proc import run_quiet

    # (OI)(CI) makes the grant inheritable by files and subdirectories, so
    # anything created inside a hardened directory is born locked down.
    rights = "(OI)(CI)(F)" if container else "(F)"
    try:
        result = run_quiet(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{_identity()}:{rights}",
            ],
            timeout=10.0,
        )
        return result.returncode == 0
    except (OSError, ValueError) as e:
        logger.debug("icacls on %s failed: %s", path, e)
        return False
    except Exception as e:  # subprocess.TimeoutExpired and friends
        logger.debug("icacls on %s failed: %s", path, e)
        return False


def harden_file(path: Path | str) -> bool:
    """Restrict ``path`` to its owner. True if the restriction was applied.

    POSIX: mode 0600. Windows: a protected DACL granting only the current user.
    """
    p = Path(path)
    key = f"f:{p}"
    if key in _done:
        return True
    try:
        if sys.platform == "win32":
            ok = _icacls(p, container=False)
        else:
            p.chmod(0o600)
            ok = True
    except OSError as e:
        logger.debug("could not harden %s: %s", p, e)
        return False
    if ok:
        _done.add(key)
    return ok


def harden_dir(path: Path | str) -> bool:
    """Restrict a directory (and, by inheritance, files created inside it).

    POSIX: mode 0700. Windows: a protected, inheritable DACL for the current
    user only. Hardening ``~/.dispatch`` itself is the cheap win — every token
    and key file written afterwards inherits it without another icacls call.
    """
    p = Path(path)
    key = f"d:{p}"
    if key in _done:
        return True
    try:
        if sys.platform == "win32":
            ok = _icacls(p, container=True)
        else:
            p.chmod(0o700)
            ok = True
    except OSError as e:
        logger.debug("could not harden dir %s: %s", p, e)
        return False
    if ok:
        _done.add(key)
    return ok


def write_private_text(path: Path | str, text: str) -> None:
    """Write a secret to ``path`` with owner-only permissions, atomically enough.

    Creates the parent directory hardened first, so on Windows the file is
    protected by inheritance from the moment it exists rather than in a second
    step after the secret has already hit the disk.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    harden_dir(p.parent)
    p.write_text(text, encoding="utf-8")
    harden_file(p)
