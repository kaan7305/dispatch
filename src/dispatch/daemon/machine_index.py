"""Machine-wide project index: lets a delegated agent start in the right
directory on ANY machine, with no sender hint, no recipient pin, and no
prior runs.

A delegated agent historically woke up in the daemon's empty scratch
workspace knowing nothing about the machine — so "summarize the yuni repo"
burned the whole run cold-searching the filesystem (and a grep from "/"
fills its head limit with /System noise before ever reaching the user's
files). This module scans the machine for project roots, caches the result,
and resolves the task to a directory deterministically:

- **resolve_cwd(task, scope_paths)** — if exactly one indexed project's name
  appears in the task text (and it lies inside the edge's path scope), that
  directory becomes the run's cwd. Conservative on purpose: zero or
  ambiguous matches resolve to None rather than guessing.
- **index_prompt(scope_paths)** — the advisory fallback when nothing
  resolved: the project list is injected into the agent's system prompt so
  its first hop is a real directory instead of a blind search.

Safety properties (mirrors daemon/memory.py):
- **Advisory, never capability.** A resolved cwd or injected entry only
  changes where the agent *looks first*. Every tool call still passes
  `can_use_tool` (scope + paths + approval); on a path-restricted edge an
  out-of-scope project is never pinned and never injected.
- **Locations only, never contents.** The index stores directory paths and
  mtimes. Nothing read from inside a project is ever stored.
- **Deterministic.** Plain marker-file scan + exact name matching — no model
  sits in the trust path.
- **Recipient-local.** Lives under ~/.dispatch/machine_index.json, never
  sent to the broker or the sender.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INDEX_FILE = Path.home() / ".dispatch" / "machine_index.json"
# Rescan at most this often; a dispatch in between reuses the cache.
INDEX_TTL_S = 15 * 60
MAX_PROJECTS = 200
# Directories *visited* cap — bounds scan time on pathological homes.
_MAX_DIRS_VISITED = 5000
# home → e.g. Desktop → repo → nested repo. Deep enough for real layouts,
# shallow enough to stay fast.
_SCAN_DEPTH = 3
# What makes a directory a "project root".
_MARKERS = (
    ".git", ".hg", "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "Gemfile", "CMakeLists.txt",
)
# Home children that are never project roots — system-owned, media, or
# package noise. Which names those are is not the same on every platform, and
# getting it wrong here is the one mistake that empties the whole index.
_SKIP_DIRS_COMMON = {
    "Library", "Applications", "Music", "Movies", "Pictures", "Public",
    "AppData", "Dropbox (Old)",
    "node_modules", "__pycache__", ".venv", "venv", ".cache", ".npm",
    "site-packages", "dist", "build",
}
# OneDrive is a sync mirror on macOS, but on Windows 11 it is where the work
# actually lives: Known Folder Move ships ON by default and relocates Desktop,
# Documents and Pictures into ~/OneDrive (or ~/"OneDrive - Employer"). Skipping
# it there skipped precisely the directories users keep projects in, and the
# failure was invisible — the scan returned an empty list, so resolve_cwd could
# never pin a cwd and every dispatch fell through to the cold "Glob the whole
# user profile" start this module exists to prevent.
_SKIP_DIRS_POSIX = {"OneDrive"}
# The Windows profile's real noise instead. Most of these are the Win9x-era
# compatibility junctions (Application Data -> AppData\Roaming) that the
# reparse-point check below also catches, but Searches, Saved Games and
# 3D Objects are ordinary directories that only a name can exclude.
_SKIP_DIRS_WINDOWS = {
    "Application Data", "Local Settings", "My Documents", "NetHood",
    "PrintHood", "Recent", "SendTo", "Start Menu", "Templates", "Cookies",
    "Searches", "3D Objects", "Saved Games", "$RECYCLE.BIN",
}
_SKIP_DIRS = _SKIP_DIRS_COMMON | (
    _SKIP_DIRS_WINDOWS if sys.platform == "win32" else _SKIP_DIRS_POSIX
)
# Hidden-ness on Windows is an attribute bit, not a leading dot, so the
# `name.startswith(".")` filter below sees none of the profile's noise: the
# legacy compatibility junctions ("Application Data" -> AppData\Roaming, "My
# Documents" -> Documents) are all HIDDEN|SYSTEM, which is what takes them out.
#
# Reparse points are deliberately NOT in this mask. Junctions are how people
# reach a projects tree that lives on another drive, and skipping them makes
# that tree invisible — the exact failure this module exists to prevent, just
# on a less common machine. The two real hazards a junction poses — walking a
# cycle forever, and indexing one tree twice under two names, which makes
# match_task refuse to pin either — are handled where they belong, by keying
# the walk on each directory's resolved identity rather than on the path used
# to reach it.
_WIN_SKIP_ATTRS = (
    0x2    # FILE_ATTRIBUTE_HIDDEN
    | 0x4  # FILE_ATTRIBUTE_SYSTEM
)
# Project names too generic to pin a cwd on — a task containing "test" or
# "docs" must not hijack the run into ~/test.
_STOP_NAMES = {
    "test", "tests", "src", "app", "apps", "docs", "doc", "code", "repo",
    "repos", "main", "data", "demo", "tmp", "temp", "new", "old", "work",
    "home", "dev", "lib", "core", "api", "web", "site", "project",
    "projects", "workspace", "folder", "files", "file", "scripts", "notes",
    "downloads", "desktop", "documents",
}


def _is_project(p: Path) -> bool:
    try:
        return any((p / m).exists() for m in _MARKERS)
    except OSError:
        return False


def _is_win_noise(entry: os.DirEntry) -> bool:
    """Is this dirent hidden, system-owned, or a reparse point? (win32 only)

    The attributes come off the directory entry Windows already handed us, so
    this costs no extra syscall — os.scandir caches them, unlike os.stat.
    """
    if sys.platform != "win32":
        return False
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & _WIN_SKIP_ATTRS)


# Shell folder IDs for SHGetKnownFolderPath.
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"


def _known_folder(folder_id: str) -> Path | None:
    """Where Windows *currently* keeps a shell folder, or None.

    ``~/Desktop`` is a guess; this is the answer. Known Folder Move rewrites
    the registry entry to a OneDrive path whose name carries the employer's
    tenant ("OneDrive - Contoso"), which no amount of joining onto the home
    directory will reproduce.
    """
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_byte * 8),
        ]

    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
    ole32.CLSIDFromString.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long

    guid = _GUID()
    if ole32.CLSIDFromString(folder_id, ctypes.byref(guid)) != 0:
        return None
    out = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, None, ctypes.byref(out)
    ) != 0:
        return None
    try:
        return Path(out.value) if out.value else None
    finally:
        # The shell allocated this with CoTaskMemAlloc; ctypes will not free it.
        ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


def _scan_roots() -> list[Path]:
    """Directories the walk starts from, outermost redirect first.

    Home alone is the right answer everywhere except a Windows profile whose
    known folders have been redirected: ~/OneDrive/Desktop sits a level deeper
    than ~/Desktop, so the same _SCAN_DEPTH reaches one level less of the
    user's actual tree. Seeding the redirected folder as its own root gives it
    back the full budget, and it comes before home so home's walk finds it
    already visited rather than re-walking it shallower.

    A known folder redirected *outside* home is deliberately not seeded: this
    index's contract, everywhere else in the module, is "projects under the
    user's home", and widening what the daemon indexes is not a portability
    fix.
    """
    home = Path.home()
    roots: list[Path] = []
    if sys.platform == "win32":
        for folder_id in (_FOLDERID_DESKTOP, _FOLDERID_DOCUMENTS):
            try:
                kf = _known_folder(folder_id)
            except (OSError, AttributeError, ValueError):
                continue
            if kf is None or kf == home or kf.parent == home:
                continue  # already reached at full depth by home's own walk
            try:
                if kf.is_relative_to(home):
                    roots.append(kf)
            except (OSError, ValueError):
                continue
    roots.append(home)
    return roots


def _scan() -> list[dict[str, Any]]:
    """Walk the home directory (bounded) and collect project roots.

    Roots can overlap — see _scan_roots — so the walk tracks where it has
    already been.
    """
    found: list[dict[str, Any]] = []
    visited = 0
    walked: set[str] = set()

    def walk(d: Path, depth: int) -> None:
        nonlocal visited
        if len(found) >= MAX_PROJECTS or visited >= _MAX_DIRS_VISITED:
            return
        # Keyed on the directory's real identity, not on the path we arrived
        # by. Overlapping roots (a redirected Desktop lives under home too) and
        # junctions both reach one tree by two names; indexing it twice is the
        # exact ambiguity match_task refuses to resolve, so it would silently
        # disable resolve_cwd for every project underneath. Resolving also
        # makes a junction cycle terminate on its second visit rather than
        # relying on _SCAN_DEPTH to run out.
        try:
            key = os.path.normcase(os.path.realpath(d))
        except OSError:
            key = os.path.normcase(str(d))
        if key in walked:
            return
        walked.add(key)
        visited += 1
        try:
            with os.scandir(d) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for c in children:
            if len(found) >= MAX_PROJECTS or visited >= _MAX_DIRS_VISITED:
                return
            try:
                if not c.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if c.name.startswith(".") or c.name in _SKIP_DIRS:
                continue
            if _is_win_noise(c):
                continue
            p = Path(c.path)
            if _is_project(p):
                try:
                    mtime = c.stat().st_mtime
                except OSError:
                    mtime = 0.0
                found.append({"path": str(p), "name": c.name, "mtime": mtime})
                # Don't descend into a project — nested repos are noise.
                continue
            if depth < _SCAN_DEPTH:
                walk(p, depth + 1)

    for root in _scan_roots():
        walk(root, 1)
    found.sort(key=lambda e: e.get("mtime", 0.0), reverse=True)
    return found


def projects(refresh: bool = False) -> list[dict[str, Any]]:
    """The machine's project index, from cache when fresh."""
    if not refresh:
        try:
            raw = json.loads(INDEX_FILE.read_text())
            if time.time() - float(raw.get("scanned_at", 0)) < INDEX_TTL_S:
                entries = raw.get("projects", [])
                if isinstance(entries, list):
                    return entries
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            pass
    projs = _scan()
    try:
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(
            json.dumps({"scanned_at": time.time(), "projects": projs}, indent=2)
        )
    except OSError:
        logger.warning("could not persist the machine index")
    return projs


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _in_scope(p: Path, scope_paths: list[str]) -> bool:
    if not scope_paths:
        return True
    for raw in scope_paths:
        d = Path(raw).expanduser()
        try:
            d = d.resolve()
            if p == d or p.is_relative_to(d):
                return True
        except OSError:
            continue
    return False


def match_task(task: str, projs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Exactly one indexed project whose (normalized) name appears as a word
    in the task → that project. Zero, generic-name, or ambiguous → None."""
    words = {
        _norm(w) for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", task)
    }
    hits_by_name: dict[str, list[dict[str, Any]]] = {}
    for p in projs:
        name = _norm(str(p.get("name", "")))
        if len(name) < 3 or name in _STOP_NAMES:
            continue
        if name in words:
            hits_by_name.setdefault(name, []).append(p)
    if len(hits_by_name) != 1:
        return None
    candidates = next(iter(hits_by_name.values()))
    if len(candidates) != 1:
        # Same name in several places — don't guess between them.
        return None
    return candidates[0]


def resolve_cwd(task: str, scope_paths: list[str]) -> Path | None:
    """The directory the run should start in, or None. Conservative: only a
    single, unambiguous, in-scope, still-existing project name match pins."""
    try:
        matched = match_task(task, projects())
    except Exception:
        logger.exception("machine index resolution failed; running without it")
        return None
    if matched is None:
        return None
    p = Path(str(matched["path"]))
    try:
        if not p.is_dir():
            return None
    except OSError:
        return None
    if not _in_scope(p.resolve(), scope_paths):
        return None
    return p


def index_prompt(scope_paths: list[str], limit: int = 30) -> str | None:
    """Advisory system-prompt block listing the machine's project dirs (most
    recently modified first), filtered to the edge's path scope. The fallback
    when resolve_cwd found nothing — the agent's first hop becomes a real
    directory instead of a blind filesystem search."""
    try:
        projs = projects()
    except Exception:
        logger.exception("machine index load failed; running without it")
        return None
    live: list[str] = []
    for p in projs:
        raw = str(p.get("path", ""))
        if not raw:
            continue
        d = Path(raw)
        try:
            if not d.is_dir() or not _in_scope(d.resolve(), scope_paths):
                continue
        except OSError:
            continue
        live.append(raw)
        if len(live) >= limit:
            break
    if not live:
        return None
    lines = "\n".join(f"- {p}" for p in live)
    return (
        "Project directories on this machine, from a local index (advisory — "
        "every tool call is still scope- and approval-gated):\n"
        f"{lines}\n"
        "If the task names a project, repository, or files, start in the "
        "matching directory above. NEVER search from the filesystem root "
        "'/' — system directories flood the results and bury the user's "
        "files. If none of these match, stop and reply asking the sender "
        "for the exact path."
    )
