"""Bundled agent runtime resolution.

The Claude Agent SDK (and Codex) do not talk to a model API directly — they
shell out to the `claude` / `codex` CLIs, which are Node.js programs. For a
non-technical recipient to run a dispatch with zero terminal setup, those
binaries (and a Node runtime) must ship *inside* the Dispatch app instead of
being installed globally with `npm -g`.

This module locates the vendored runtime — bundled under the app's Resources
when frozen (PyInstaller), or a repo-local `vendor/` tree in development — and
prepends its `bin/` to PATH so that:

  * the Agent SDK's ``shutil.which("claude")`` finds our vendored CLI, and
  * that CLI's ``#!/usr/bin/env node`` shim resolves to our vendored Node
    (because the same ``bin/`` dir is first on PATH).

Call :func:`prepare_agent_runtime` once, early, in any process that will spawn
an agent. It is idempotent and cheap after the first call.

Layout of the vendored tree (produced by ``scripts/vendor_agents.py``)::

    vendor/
      bin/            node, claude, codex   (this dir goes on PATH)
      lib/            node_modules for the CLIs
      NOTICE          third-party licenses

Credentials are handled elsewhere: the daemon exports ``ANTHROPIC_API_KEY``
from ``~/.dispatch/config.json`` (the "paste a key" path), and the vendored
``claude``/``codex`` CLIs honor their own OAuth login state in
``~/.claude.json`` / the Codex config dir (the "sign in with your
subscription" path). This module only makes the binaries reachable.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_prepared = False

# Where a first-launch download fallback drops the runtime for a per-user
# install (see scripts/vendor_agents.py --dest, and the tray bootstrap).
APP_SUPPORT_VENDOR = (
    Path.home() / "Library" / "Application Support" / "Dispatch" / "vendor"
)


def _candidate_roots() -> list[Path]:
    """Every place a vendored runtime might live, most-specific first."""
    roots: list[Path] = []

    # 1. Frozen app: PyInstaller unpacks bundled `datas` under _MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "vendor")

    # 2. Alongside a frozen executable: the COLLECT dir, and the .app's
    #    Contents/Resources (where a signed build keeps executable helpers).
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots.append(exe_dir / "vendor")
        roots.append(exe_dir.parent / "Resources" / "vendor")

    # 3. Per-user install (first-launch download fallback lands here).
    roots.append(APP_SUPPORT_VENDOR)

    # 4. Development: a `vendor/` tree at the repo root.
    #    runtime.py -> executor -> dispatch -> src -> <repo root>
    repo_root = Path(__file__).resolve().parents[3]
    roots.append(repo_root / "vendor")

    return roots


def _exe(bindir: Path, name: str) -> bool:
    """True if `name` (or `name.exe`) exists in bindir."""
    return (bindir / name).exists() or (bindir / f"{name}.exe").exists()


def find_runtime() -> Path | None:
    """Return the first vendored runtime root that actually contains a Node
    plus a `claude` CLI, or None if no bundled runtime is present."""
    for root in _candidate_roots():
        bindir = root / "bin"
        if _exe(bindir, "node") and _exe(bindir, "claude"):
            return root
    return None


def prepare_agent_runtime() -> bool:
    """Put the vendored agent CLIs on PATH for this process (idempotent).

    Returns True if a bundled runtime was found and wired up. Returns False
    when none is bundled — in which case the SDK falls back to whatever
    ``claude`` is on the ambient PATH (a developer machine with a global
    install). On an end-user machine, a False here is the exact failure the
    bundled installer exists to prevent, so it is logged loudly.
    """
    global _prepared
    if _prepared:
        return True

    root = find_runtime()
    if root is None:
        if not shutil.which("claude"):
            log.warning(
                "no bundled agent runtime found and no `claude` on PATH — "
                "dispatches cannot run until an agent CLI is available. "
                "Run scripts/vendor_agents.py, or install the bundled app."
            )
        return False

    bindir = str((root / "bin").resolve())
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if bindir not in parts:
        os.environ["PATH"] = os.pathsep.join([bindir, *parts])

    # A breadcrumb the local UI / logs can read to show "what's installed".
    os.environ.setdefault("DISPATCH_AGENT_RUNTIME", str(root.resolve()))

    _prepared = True
    log.info("agent runtime ready: %s", bindir)
    return True


def runtime_status() -> dict:
    """Summary for the local UI's 'what's installed' panel. Reports whether
    each agent is reachable, whether via the bundle or an ambient install."""
    root = find_runtime()
    bindir = (root / "bin") if root else None

    def _have(name: str) -> bool:
        return bool(bindir and _exe(bindir, name)) or bool(shutil.which(name))

    return {
        "bundled": root is not None,
        "root": str(root) if root else None,
        "node": _have("node"),
        "claude": _have("claude"),
        "codex": _have("codex"),
    }
