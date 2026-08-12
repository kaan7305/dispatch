"""Filename rules Win32 enforces that the POSIX path model cannot express.

A workflow's ``file.write`` / ``context`` nodes take a sender-authored path,
join it to the recipient's workspace, and confirm with ``relative_to`` that the
result stayed inside. On POSIX that check is the whole story: every byte other
than ``/`` and NUL is an ordinary filename character, so a path that resolves
inside the workspace addresses a file inside the workspace.

Win32 breaks that equivalence in ways that pass the containment check because
they are legal *path syntax*:

  • **Alternate data streams.** ``report.txt:hidden`` writes to a second,
    invisible stream of ``report.txt``. Nothing lists it, and a later
    ``file.read`` of ``report.txt`` sees none of it.
  • **Trailing dots and spaces.** Win32 strips them on the way to the
    filesystem, so ``report.`` and ``report `` and ``report`` are the same
    file. Two workflow nodes that believe they wrote different files silently
    clobber each other, and neither name appears in a directory listing.
  • **Drive-relative paths.** ``a:b.txt`` resolves against the current
    directory *of drive A*, which is nowhere near the workspace.
  • **Device names.** ``<workspace>/NUL`` is the null device, not a file:
    ``Path("NUL").write_bytes(b"hello world!!")`` returns 13 and ``exists()``
    returns True while the bytes go nowhere.

Only the first three are decided here, by inspecting the name. Device names are
deliberately **not** on that list, because the folklore table — CON, PRN, AUX,
NUL, COM1–9, LPT1–9, matched against the text before the first dot — does not
describe how Windows 11 actually behaves. Measured on a stock Windows 11 26200
machine, writing into an ordinary directory:

    NUL        -> device: reports 12 bytes written, reads back empty, not listed
    CON  AUX  PRN  COM1  LPT1        -> ordinary files, round-trip intact
    nul.txt  aux.txt  con.md  com1.csv  -> ordinary files, round-trip intact

So the table's only true positive here is bare ``NUL``, and rejecting the rest
would break ``com1.csv`` and ``con.md`` — plausible filenames a workflow has
every right to write. Which names are still devices varies by Windows version
and by how the path is opened, so predicting it is the wrong shape of solution
entirely.

:func:`verify_written` takes the other approach: write the file, then check the
bytes actually landed under the name we used. That catches the null device,
catches any name the filesystem mangled, catches a future Windows build
reviving a device name we would not have guessed — and never rejects a name
that works.

Everything here is a no-op off Windows. macOS has none of these rules — ``NUL``
there is just a file — and rejecting the name would change what a workflow that
works today is allowed to do.
"""
from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath
from typing import Optional, Union

_IS_WINDOWS = sys.platform == "win32"

# The Win32 path limit, including the drive letter and the terminating NUL —
# so the longest usable path is MAX_PATH - 1 characters.
MAX_PATH = 260


class WindowsPathError(ValueError):
    """A path that is syntactically fine but does not mean what it says on
    Windows. Callers translate this into whatever error their layer speaks."""


def _component_reason(part: str) -> Optional[str]:
    """Why this single path component is unusable, or None if it is fine."""
    if part in (".", ".."):
        return None  # navigation, not a name; the trailing-dot rule is not about these

    if ":" in part:
        return (
            f"{part!r} names an NTFS alternate data stream; the text after ':' "
            f"is written to a hidden stream, not to a file"
        )

    if part != part.rstrip(". "):
        return (
            f"{part!r} ends in a dot or space, which Windows strips — it would "
            f"collide with {part.rstrip('. ')!r}"
        )
    return None


def reserved_reason(name_or_path: Union[str, "PureWindowsPath"]) -> Optional[str]:
    """Why Windows would misinterpret this path, or None if it is safe.

    Reports on the whole path, component by component, so a reserved name
    buried in a subdirectory (``notes/COM1/out.txt``) is caught too. Uses
    ``PureWindowsPath`` unconditionally, so the rules are inspectable (and
    testable) from any platform — it is :func:`reject_reserved` that is inert
    off Windows, not the analysis.
    """
    text = str(name_or_path)
    if not text:
        return None
    path = PureWindowsPath(text)

    # A drive letter with no root ("a:b.txt") is Win32's drive-relative form:
    # it points at the current directory *of drive A*, which is nowhere near
    # the workspace. It is also how an alternate data stream is spelled when
    # the file name happens to be one character long, which is why it has to
    # be rejected before the per-component ':' check drops the anchor.
    if path.drive and not path.root:
        return (
            f"{text!r} is drive-relative — Windows resolves it against the "
            f"current directory of drive {path.drive}, not the workspace"
        )

    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        reason = _component_reason(part)
        if reason is not None:
            return reason
    return None


def reject_reserved(name_or_path: Union[str, "PureWindowsPath"]) -> None:
    """Raise :class:`WindowsPathError` if Windows would misread this path.

    No-op on every other platform.
    """
    if not _IS_WINDOWS:
        return
    reason = reserved_reason(name_or_path)
    if reason is not None:
        raise WindowsPathError(reason)


def verify_written(path: Union[str, "Path"], expected_bytes: int) -> None:
    """Confirm a just-written file really holds ``expected_bytes`` under its name.

    The check a device name cannot survive: ``<workspace>/NUL`` accepts the
    write and reports the byte count, but stat-ing it back gives size 0 and it
    never appears in its parent's listing. The same check catches a name the
    filesystem silently rewrote (a trailing dot or space that slipped past the
    syntax rules above, a character the volume folded), which is the other way
    a caller ends up holding a path to a file that is not there.

    Raises :class:`WindowsPathError` describing what was actually found. No-op
    off Windows, where a successful write means the bytes are in the file.
    """
    if not _IS_WINDOWS:
        return
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise WindowsPathError(
            f"wrote {expected_bytes} bytes to {target.name!r} but the file "
            f"cannot be read back ({exc.__class__.__name__}); on Windows this "
            f"means the name resolved to a device rather than a file"
        ) from exc

    if size != expected_bytes:
        raise WindowsPathError(
            f"wrote {expected_bytes} bytes to {target.name!r} but the file "
            f"holds {size}; on Windows this means the name resolved to a "
            f"device (NUL discards writes while reporting success) rather "
            f"than to a file in the workspace"
        )

    try:
        listed = any(entry.name == target.name for entry in target.parent.iterdir())
    except OSError:
        return  # cannot list the directory; the size check already passed
    if not listed:
        raise WindowsPathError(
            f"wrote {expected_bytes} bytes but {target.name!r} does not appear "
            f"in {target.parent.name or 'the workspace'}; Windows stored it "
            f"under a different name, so a later read of this path will miss it"
        )


def _long_paths_enabled() -> bool:
    """Has this machine opted out of MAX_PATH?

    Windows 10 1607 and later honour paths beyond MAX_PATH when the machine
    sets ``LongPathsEnabled`` *and* the process declares itself long-path
    aware, which CPython's manifest does. Checking is worth the registry read:
    without it we would reject paths that this particular machine handles
    perfectly well.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except (ImportError, OSError, ValueError):
        return False


def reject_too_long(name_or_path: Union[str, "PureWindowsPath"]) -> None:
    """Raise :class:`WindowsPathError` if the path exceeds Win32's MAX_PATH.

    The alternative — rewriting the path to the ``\\\\?\\`` form that bypasses
    the limit — changes normalisation rules for the whole path and is out of
    proportion to a workflow node writing a scratch file. A named limit in the
    error is far more useful to the sender than the bare ``[WinError 3] The
    system cannot find the path specified`` they get today, which reads like
    the directory is missing.

    No-op on every other platform.
    """
    if not _IS_WINDOWS:
        return
    text = str(name_or_path)
    if len(text) < MAX_PATH:
        return
    if _long_paths_enabled():
        return
    raise WindowsPathError(
        f"path is {len(text)} characters, over the Windows MAX_PATH limit of "
        f"{MAX_PATH} (including the terminating NUL); shorten the path or "
        f"enable LongPathsEnabled on this machine"
    )
