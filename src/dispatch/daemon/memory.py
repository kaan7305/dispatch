"""Per-capability-bucket machine memory: cross-dispatch context that kills the
cold-start filesystem search.

A delegated agent wakes up in an empty workspace knowing nothing about this
machine — so "explain your yuni repo" historically burned half the run just
*finding* the repo. This module remembers durable machine facts (project
directory locations) across runs and injects them into the next run's system
prompt.

Keying — Option A, implicit "trust categories": edges whose **capability
envelope** (tools + mcp + paths + approval — the fields that define what an
agent may do) is identical share one memory bucket. Recipients tend to grant
the same few scope shapes to many people; everyone in the same shape benefits
from facts any of them surfaced. Operational fields (`auto_tools`,
`max_dispatches_per_day`, `expires_at`, `result_visibility`, `sync`) are
excluded from the hash: they don't change what's discoverable, and they drift
per-edge, which would needlessly fragment buckets.

Safety properties (the reasons this is OK to share across senders):
- **Advisory, never capability.** Memory only changes what the agent *tries
  first*. Every tool call still goes through `can_use_tool` + approval; a
  stale or poisoned entry costs at most one denied call.
- **Locations only, never contents.** Entries are directory paths + metadata.
  Nothing read from a file is ever stored.
- **Deterministic harvest.** Facts come from successful tool-call *inputs*
  (which directories were actually accessed), not from an LLM pass — no model
  sits in the trust path.
- **Recipient-local.** Lives under ~/.dispatch/memory/, never sent to the
  broker or the sender.
- **Filtered at injection time.** Entries outside the current edge's `paths`
  scope are withheld (not deleted) — narrowing a scope instantly hides them,
  re-widening restores them.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dispatch.shared.schema import DispatchEvent, Scopes

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / ".dispatch" / "memory"
# Cap per bucket — these are project roots, not a database. Oldest-used fall off.
MAX_ENTRIES = 50
# How far repo-root detection walks up before giving up.
_MAX_WALK_UP = 8


def capability_bucket(scope: Scopes) -> str:
    """Stable id for the scope's capability envelope. Edges with identical
    envelopes (≈ the recipient's implicit 'trust category') share a bucket."""
    envelope = {
        "tools": sorted(scope.tools or []),
        "mcp": sorted(scope.mcp or []),
        "paths": sorted(str(Path(p).expanduser()) for p in (scope.paths or [])),
        "approval": scope.approval,
    }
    canon = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _bucket_file(bucket: str) -> Path:
    return MEMORY_DIR / f"{bucket}.json"


def load_entries(bucket: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(_bucket_file(bucket).read_text())
        entries = raw.get("entries", [])
        return entries if isinstance(entries, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_entries(bucket: str, entries: list[dict[str, Any]]) -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _bucket_file(bucket).write_text(
            json.dumps({"version": 1, "entries": entries}, indent=2)
        )
    except OSError:
        logger.warning("could not persist dispatch memory for bucket %s", bucket)


def repo_root(path: Path) -> Path | None:
    """Walk up from `path` to the enclosing git repo root, if any. Bounded,
    and never returns the home directory or filesystem root themselves."""
    home = Path.home()
    p = path if path.is_dir() else path.parent
    for _ in range(_MAX_WALK_UP):
        if p == home or p == p.parent:
            return None
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


class RunHarvester:
    """Observes a run's event stream and, at the end, distills durable machine
    facts (project roots that were successfully accessed) into the bucket.

    Deterministic: pairs successful tool_results back to their tool_use inputs
    and takes only the path arguments — no LLM, no result contents."""

    _PATH_KEYS = ("file_path", "path", "cwd", "notebook_path")

    def __init__(self) -> None:
        self._inputs: dict[str, dict[str, Any]] = {}
        self._ok_paths: list[str] = []

    def observe(self, event: DispatchEvent) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "tool_use":
            tid = data.get("id")
            tool_input = data.get("input")
            if isinstance(tid, str) and isinstance(tool_input, dict):
                self._inputs[tid] = tool_input
        elif etype == "tool_result" and not data.get("is_error"):
            tool_input = self._inputs.pop(data.get("tool_use_id") or "", None)
            if tool_input:
                for key in self._PATH_KEYS:
                    v = tool_input.get(key)
                    if isinstance(v, str) and v.startswith(("/", "~")):
                        self._ok_paths.append(v)

    def finish(self, bucket: str, run_cwd: Path | None = None) -> None:
        """Collapse accessed paths to repo roots and merge into the bucket.
        `run_cwd` (a directory the recipient explicitly pinned at accept time)
        is recorded directly — the human vouched for it, strongest signal."""
        roots: set[str] = set()
        for raw in self._ok_paths:
            try:
                root = repo_root(Path(raw).expanduser().resolve())
            except OSError:
                continue
            if root is not None:
                roots.add(str(root))
        if run_cwd is not None and run_cwd != Path.home():
            roots.add(str(run_cwd))
        if not roots:
            return

        now = datetime.now(timezone.utc).isoformat()
        entries = load_entries(bucket)
        by_path = {e.get("path"): e for e in entries if isinstance(e, dict)}
        for root in roots:
            if root in by_path:
                by_path[root]["last_seen"] = now
                by_path[root]["hits"] = int(by_path[root].get("hits", 1)) + 1
            else:
                by_path[root] = {
                    "path": root,
                    "kind": "project_dir",
                    "first_seen": now,
                    "last_seen": now,
                    "hits": 1,
                }
        merged = sorted(
            by_path.values(), key=lambda e: e.get("last_seen", ""), reverse=True
        )[:MAX_ENTRIES]
        _save_entries(bucket, merged)


def memory_prompt(
    entries: list[dict[str, Any]],
    allowed_dirs: list[Path],
    paths_restricted: bool,
) -> str | None:
    """The advisory context block for the executor's system prompt, or None.

    Re-validated at injection time: directories that no longer exist are
    skipped, and on a path-restricted edge anything outside the CURRENT
    allowlist is withheld — memory learned under a wider scope must not leak
    into a narrower one."""
    live: list[dict[str, Any]] = []
    for e in entries:
        raw = e.get("path")
        if not isinstance(raw, str):
            continue
        p = Path(raw)
        try:
            if not p.is_dir():
                continue
            if paths_restricted and not any(
                p == d or p.is_relative_to(d) or d.is_relative_to(p)
                for d in allowed_dirs
            ):
                continue
        except OSError:
            continue
        live.append(e)
    if not live:
        return None
    lines = "\n".join(
        f"- {e['path']} (last used {str(e.get('last_seen', ''))[:10]})" for e in live
    )
    return (
        "Known project directories on this machine, learned from previous "
        "delegated runs (advisory — every tool call is still scope- and "
        "approval-gated; do not assume access beyond your granted tools):\n"
        f"{lines}\n"
        "If the task names a project that matches one of these, start there "
        "instead of searching the filesystem."
    )
