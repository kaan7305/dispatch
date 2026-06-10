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
import re
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
# package noise (covers macOS / Linux / Windows homes).
_SKIP_DIRS = {
    "Library", "Applications", "Music", "Movies", "Pictures", "Public",
    "AppData", "OneDrive", "Dropbox (Old)",
    "node_modules", "__pycache__", ".venv", "venv", ".cache", ".npm",
    "site-packages", "dist", "build",
}
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


def _scan() -> list[dict[str, Any]]:
    """Walk the home directory (bounded) and collect project roots."""
    home = Path.home()
    found: list[dict[str, Any]] = []
    visited = 0

    def walk(d: Path, depth: int) -> None:
        nonlocal visited
        if len(found) >= MAX_PROJECTS or visited >= _MAX_DIRS_VISITED:
            return
        visited += 1
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for c in children:
            if len(found) >= MAX_PROJECTS or visited >= _MAX_DIRS_VISITED:
                return
            try:
                if not c.is_dir() or c.is_symlink():
                    continue
            except OSError:
                continue
            if c.name.startswith(".") or c.name in _SKIP_DIRS:
                continue
            if _is_project(c):
                try:
                    mtime = c.stat().st_mtime
                except OSError:
                    mtime = 0.0
                found.append({"path": str(c), "name": c.name, "mtime": mtime})
                # Don't descend into a project — nested repos are noise.
                continue
            if depth < _SCAN_DEPTH:
                walk(c, depth + 1)

    walk(home, 1)
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
