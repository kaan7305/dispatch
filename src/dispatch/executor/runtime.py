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

Note that PATH is only the SDK's *fallback*. claude-agent-sdk >= 0.2.x ships a
self-contained CLI at ``claude_agent_sdk/_bundled/claude{.exe}`` on the wheels
that have one, and ``_find_cli`` returns it before consulting PATH at all — so
on those installs this module's PATH work decides nothing about which CLI runs.
It still decides what we *report*, which is why the checks below have to be
honest about executability rather than mere presence.

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

from dispatch.shared import proc

log = logging.getLogger(__name__)

_prepared = False


def _app_support_vendor() -> Path:
    """The per-user install location for a downloaded runtime, per platform.

    Each OS has exactly one directory an unprivileged installer is allowed to
    write app data into, and they are not interchangeable: a Windows machine
    has no ``~/Library``, so the macOS path resolved to a directory that could
    never exist and the per-user install slot silently dropped out of the
    candidate list.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Dispatch" / "vendor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Dispatch" / "vendor"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "Dispatch" / "vendor"


# Where a first-launch download fallback drops the runtime for a per-user
# install (see scripts/vendor_agents.py --dest, and the tray bootstrap).
APP_SUPPORT_VENDOR = _app_support_vendor()


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

    # 3. Per-user install (first-launch download fallback lands here). Resolved
    #    per call, not from the module constant, so a relocated %LOCALAPPDATA%
    #    / $XDG_DATA_HOME is honoured rather than frozen at import time.
    roots.append(_app_support_vendor())

    # 4. Development: a `vendor/` tree at the repo root.
    #    runtime.py -> executor -> dispatch -> src -> <repo root>
    repo_root = Path(__file__).resolve().parents[3]
    roots.append(repo_root / "vendor")

    return roots


# These two live in dispatch.shared.proc so that `dispatch doctor` and
# codex.py can ask "is there a runnable CLI here?" without importing this
# package — dispatch.executor.__init__ pulls in the whole Agent SDK. Re-exported
# under the private names the rest of this module already uses.
_NATIVE_SUFFIXES = proc.NATIVE_SUFFIXES
_is_native_image = proc.is_native_image
_native_exe = proc.native_exe


def _exe(bindir: Path, name: str) -> bool:
    """True if `bindir` holds a `name` the OS can actually execute.

    The Windows arm is the whole point. ``npm i -g @anthropic-ai/claude-code``
    writes three files — ``claude`` (a POSIX shell shim), ``claude.cmd`` and
    ``claude.ps1`` — and none of them is an image CreateProcess can spawn. A
    plain existence test therefore reported a healthy runtime on a machine
    where every dispatch failed at spawn, and the failure surfaced only inside
    the agent session: find_runtime() said yes, the UI went green, and the SDK
    refused the .cmd shim later where nobody was reading.
    """
    if sys.platform == "win32":
        if _is_native_image(name):
            return (bindir / name).is_file()
        return any((bindir / f"{name}{sfx}").is_file() for sfx in _NATIVE_SUFFIXES)
    return (bindir / name).exists() or (bindir / f"{name}.exe").exists()


def _shim_only(name: str) -> bool:
    """PATH resolves `name`, but to something that cannot be executed.

    This is the state that used to be invisible: the machine looks equipped
    from every angle a `which` can see, and only a spawn tells the truth.
    """
    return shutil.which(name) is not None and _native_exe(name) is None


def find_runtime() -> Path | None:
    """Return the first vendored runtime root with a usable `claude` CLI, or
    None if no bundled runtime is present.

    A sibling Node is part of the gate wherever the vendored ``claude`` is a JS
    entry point behind a ``#!/usr/bin/env node`` shim — which is the whole
    reason this module prepends ``bin/`` to PATH (see the module docstring).
    There, a root with no Node is genuinely unusable: the shim resolves ``node``
    off the ambient PATH or not at all, and accepting the root would put a
    broken CLI ahead of a working global install.

    Windows is the exception, and only Windows: the native CLI is a
    self-contained ``.exe`` with no interpreter to find, so requiring a sibling
    Node there rejects roots that work perfectly. Node is reported separately
    by :func:`runtime_status`. A root carrying both still wins on every
    platform, so a tree vendored the old way resolves exactly as before.
    """
    claude_only: Path | None = None
    for root in _candidate_roots():
        bindir = root / "bin"
        if not _exe(bindir, "claude"):
            continue
        if _exe(bindir, "node"):
            return root
        if sys.platform == "win32" and claude_only is None:
            claude_only = root
    return claude_only


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
        if _shim_only("claude"):
            log.warning(
                "a `claude` shim is on PATH but it is not an executable image "
                "(%s) — npm's Windows install writes claude/claude.cmd/claude.ps1, "
                "none of which can be spawned, so every dispatch will fail at "
                "launch. Install the native claude.exe: irm https://claude.ai/"
                "install.ps1 | iex",
                shutil.which("claude"),
            )
        elif not _native_exe("claude"):
            log.warning(
                "no bundled agent runtime found and no `claude` on PATH — "
                "dispatches cannot run until an agent CLI is available. "
                "Run scripts/vendor_agents.py, or install the bundled app."
            )
        return False

    bindir = str((root / "bin").resolve())
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    # normcase, because Windows PATH entries name the same directory in
    # different case and with either separator. A literal string compare read
    # an entry that was already present as absent and prepended a duplicate.
    folded = os.path.normcase(bindir)
    if not any(os.path.normcase(p) == folded for p in parts):
        os.environ["PATH"] = os.pathsep.join([bindir, *parts])

    # A breadcrumb the local UI / logs can read to show "what's installed".
    os.environ.setdefault("DISPATCH_AGENT_RUNTIME", str(root.resolve()))

    _prepared = True
    log.info("agent runtime ready: %s", bindir)
    return True


# Per-CLI resolution states reported by runtime_status(). "shim_only" exists
# because it used to be indistinguishable from "path": a PATH lookup succeeded,
# so the panel showed a tick, and the only place the truth appeared was a spawn
# failure inside an agent session nobody was watching. It is a distinct state,
# not a flavour of missing, because the remediation is specific — the user has
# the CLI installed, just in a form Windows cannot start.
STATE_BUNDLED = "bundled"
STATE_PATH = "path"
STATE_SHIM_ONLY = "shim_only"
STATE_MISSING = "missing"

_SHIM_ADVICE = {
    "claude": (
        "install the native claude.exe (PowerShell: irm https://claude.ai/"
        "install.ps1 | iex); the npm package installs a claude.cmd shim that "
        "cannot be spawned"
    ),
    "codex": (
        "install the native codex.exe; the npm package installs a codex.cmd "
        "shim that cannot be spawned"
    ),
    "node": "install Node from nodejs.org rather than a .cmd shim",
}


def _resolve_state(name: str, bindir: Path | None) -> str:
    if bindir is not None and _exe(bindir, name):
        return STATE_BUNDLED
    if _native_exe(name):
        return STATE_PATH
    if shutil.which(name):
        return STATE_SHIM_ONLY
    return STATE_MISSING


def runtime_status() -> dict:
    """Summary for the local UI's 'what's installed' panel and `dispatch
    doctor`. Reports whether each agent is reachable, whether via the bundle or
    an ambient install — and, when it is neither, which of the two very
    different reasons applies.

    The booleans keep their original meaning ("can this actually run") for
    existing callers; `states` carries the detail and `shims` names the CLIs
    that are installed but unrunnable, so the UI can say so instead of
    rendering the same red tick it would for a machine with nothing on it.
    """
    root = find_runtime()
    bindir = (root / "bin") if root else None

    states = {name: _resolve_state(name, bindir) for name in ("node", "claude", "codex")}
    shims = [name for name, state in states.items() if state == STATE_SHIM_ONLY]

    return {
        "bundled": root is not None,
        "root": str(root) if root else None,
        "node": states["node"] in (STATE_BUNDLED, STATE_PATH),
        "claude": states["claude"] in (STATE_BUNDLED, STATE_PATH),
        "codex": states["codex"] in (STATE_BUNDLED, STATE_PATH),
        "states": states,
        "shims": shims,
        "warnings": [
            f"`{name}` is on PATH at {shutil.which(name)} but is not an "
            f"executable image — {_SHIM_ADVICE.get(name, 'install a native build')}"
            for name in shims
        ],
    }
