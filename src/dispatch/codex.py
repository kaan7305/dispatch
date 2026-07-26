"""Codex host integration — the same Dispatch front door, hosted by Codex CLI.

Dispatch's front door is host-neutral by construction: an MCP server
(`dispatch-mcp`, a thin client of the local daemon) plus one skill. Claude Code
picks both up from `.claude-plugin/plugin.json`; Codex picks the *same*
`skills/` directory and the *same* `dispatch-mcp` command up from
`.codex-plugin/plugin.json`. Neither host holds a key, a broker connection, or
an executor — so "which agent am I sitting in" changes nothing about how a
dispatch is authorized or run.

This module covers the two places Codex genuinely is not interchangeable:

  1. **Install.** Codex has no `~/.claude.json`. Its MCP servers live in
     `$CODEX_HOME/config.toml` under `[mcp_servers.<name>]` and its personal
     skills in `$CODEX_HOME/skills/<name>/`. `dispatch codex install` writes
     both, for the pipx user who never touches a plugin marketplace.

  2. **Discovery.** The invite/edit-permissions picker offers the recipient
     their *own* installed MCP servers to scope onto a trust edge. That pool is
     auto-discovered, and a Codex user's servers are TOML, not JSON — so
     without this the pool comes back empty and a Codex recipient cannot grant
     an MCP tool at all.

Everything here is best-effort and read-mostly: a malformed host config yields a
smaller pool, never an exception that reaches the daemon. Nothing here widens a
grant — it only reports what the machine already has installed.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("dispatch.codex")

# The MCP server entry we write into the user's Codex config. Two non-default
# timeouts matter, and both are load-bearing rather than cosmetic:
#
#   startup_timeout_sec — `dispatch-mcp`'s lifespan ENSURES a daemon is running,
#     spawning one and waiting up to DAEMON_BOOT_TIMEOUT_S (30s) for it to bind
#     the loopback API. That wait happens during MCP `initialize`, so a client
#     whose startup budget is the usual ~10s would mark the server failed on the
#     very first launch — the one launch that always has to spawn a daemon.
#
#   tool_timeout_sec — accepting a dispatch supervises the run to completion
#     (_SUPERVISE_TIMEOUT_S = 600s), and each Layer-3 approval gives the human
#     ~120s to answer. A single `dispatch_act(action="accept")` call can
#     legitimately occupy ten minutes, so the default per-tool budget would kill
#     a healthy run mid-flight.
SERVER_NAME = "dispatch"
SERVER_COMMAND = "dispatch-mcp"
STARTUP_TIMEOUT_S = 45
TOOL_TIMEOUT_S = 900

SKILL_NAME = "dispatch"


def codex_home() -> Path:
    """Codex's config root — `$CODEX_HOME`, else `~/.codex`."""
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def config_path() -> Path:
    return codex_home() / "config.toml"


def skills_dir() -> Path:
    return codex_home() / "skills"


def codex_installed() -> bool:
    """Is the `codex` CLI on PATH, or has it at least written a config? Either
    is enough to treat this machine as a Codex machine (the vendored runtime
    puts `codex` on PATH without the user ever running it themselves)."""
    return shutil.which("codex") is not None or config_path().exists()


# ----------------------------------------------------------------------------
# TOML reading. tomllib is stdlib on 3.11+; tomli backfills 3.10 (declared as a
# dependency). If neither is importable we degrade to an empty read rather than
# failing — a missing parser must not cost the daemon its MCP pool *and* raise.
# ----------------------------------------------------------------------------

def _toml_loader():
    try:
        import tomllib
        return tomllib.loads
    except ModuleNotFoundError:
        pass
    try:
        import tomli
        return tomli.loads
    except ModuleNotFoundError:
        logger.debug("no TOML parser available; Codex config will not be read")
        return None


def read_config(path: Optional[Path] = None) -> dict:
    """Parse a Codex `config.toml`. Returns {} for missing/unreadable/malformed
    files — every caller treats an empty config as "nothing installed"."""
    target = path or config_path()
    loads = _toml_loader()
    if loads is None:
        return {}
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        parsed = loads(raw)
    except Exception as exc:  # noqa: BLE001 — any parse error degrades to empty
        logger.debug("could not parse %s: %s", target, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ----------------------------------------------------------------------------
# Discovery: Codex's installed MCP servers, in the shape the rest of Dispatch
# already speaks (a Claude `.mcp.json` entry).
# ----------------------------------------------------------------------------

def _normalize_server(cfg: dict) -> Optional[dict]:
    """Translate one `[mcp_servers.<name>]` table into the pool's entry shape.

    Codex and Claude describe the same two transports with different keys, and
    the pool is consumed by the executor and by mcp_introspect — both of which
    read the Claude shape. So we translate here, once, rather than teaching
    every consumer a second dialect:

      stdio  command/args/env            → command/args/env (identical)
      http   url + bearer_token_env_var  → url + Authorization header

    `bearer_token_env_var` names an env var rather than carrying the secret, and
    we resolve it from the daemon's own environment. If it isn't set we emit the
    server without the header: the handshake then fails with a real reason and
    the permissions dialog degrades to a whole-server checkbox, which beats
    silently sending `Bearer None`. Server configs never cross the local API
    boundary (see local_app.list_mcp_servers), so a resolved token stays in the
    daemon process that launched the server.

    Returns None for an entry that names no transport, or one the user disabled.
    """
    if not isinstance(cfg, dict):
        return None
    if cfg.get("enabled") is False:
        return None

    out: dict[str, Any] = {}
    command = cfg.get("command")
    url = cfg.get("url")

    if isinstance(command, str) and command.strip():
        out["command"] = command
        args = cfg.get("args")
        if isinstance(args, list):
            out["args"] = [str(a) for a in args]
        env = cfg.get("env")
        if isinstance(env, dict):
            out["env"] = {str(k): str(v) for k, v in env.items()}
    elif isinstance(url, str) and url.strip():
        out["type"] = "http"
        out["url"] = url
        var = cfg.get("bearer_token_env_var")
        if isinstance(var, str) and var.strip():
            token = os.environ.get(var.strip())
            if token:
                out["headers"] = {"Authorization": f"Bearer {token}"}
            else:
                logger.debug(
                    "codex mcp server declares bearer_token_env_var=%s but it is "
                    "unset in this process; emitting the server unauthenticated",
                    var,
                )
    else:
        return None

    return out


def _absorb(servers: Any, into: dict[str, dict]) -> None:
    """Merge a `[mcp_servers]` table into the accumulator, first sighting wins.

    `dispatch` itself is never discoverable: handing a dispatch the Dispatch
    control plane would let task text re-wield the recipient's identity to
    dispatch onward (the same reason daemon.main withholds the dispatch tools
    from the executor)."""
    if not isinstance(servers, dict):
        return
    for name, cfg in servers.items():
        if not isinstance(name, str) or name.strip().lower() == SERVER_NAME:
            continue
        if name in into:
            continue
        entry = _normalize_server(cfg)
        if entry is not None:
            into[name] = entry


def discover_codex_mcp() -> dict[str, dict]:
    """Auto-discover the MCP servers this machine's Codex has installed.

    Two scopes, merged by name with global winning (mirrors how the Claude scan
    prefers user scope over project scope):
      - global:  `$CODEX_HOME/config.toml` → `[mcp_servers.<name>]`
      - project: for each `[projects."<path>"]` Codex has recorded, that
                 project's own `<path>/.codex/config.toml`

    Best-effort throughout: any unreadable or malformed source is skipped, so
    the worst case is a shorter picker rather than a broken dialog."""
    found: dict[str, dict] = {}
    root = read_config()
    _absorb(root.get("mcp_servers"), found)

    projects = root.get("projects")
    if isinstance(projects, dict):
        for path in projects:
            if not isinstance(path, str) or not path.strip():
                continue
            try:
                scoped = read_config(Path(path).expanduser() / ".codex" / "config.toml")
            except (OSError, ValueError):
                continue
            _absorb(scoped.get("mcp_servers"), found)
    return found


# ----------------------------------------------------------------------------
# Install: write the MCP server entry + the skill into the user's Codex home.
# ----------------------------------------------------------------------------

def render_mcp_block() -> str:
    """The exact TOML we append for `[mcp_servers.dispatch]`."""
    return (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f'command = "{SERVER_COMMAND}"\n'
        f"startup_timeout_sec = {STARTUP_TIMEOUT_S}\n"
        f"tool_timeout_sec = {TOOL_TIMEOUT_S}\n"
    )


def server_entry(config: Optional[dict] = None) -> Optional[dict]:
    """The `[mcp_servers.dispatch]` table already in the user's config, if any."""
    root = config if config is not None else read_config()
    servers = root.get("mcp_servers")
    if isinstance(servers, dict):
        entry = servers.get(SERVER_NAME)
        if isinstance(entry, dict):
            return entry
    return None


def _section_bounds(lines: list[str], header: str) -> Optional[tuple[int, int]]:
    """Line range [start, end) of a TOML section, given its exact header text.

    Used only to *replace* our own `[mcp_servers.dispatch]` block on --force. A
    section runs from its header to the next line that opens another table at
    any depth, which is all we need: we never rewrite a table we didn't write.
    Returns None when the header isn't present."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        # Any following table header ends our section — '[x]' or '[[x]]' alike.
        if lines[j].lstrip().startswith("["):
            return start, j
    return start, len(lines)


def find_skill_source() -> Optional[Path]:
    """Locate `skills/dispatch/` on this machine, without touching the network.

    The skill ships in the *plugin* bundle, not the pip wheel, so where it lives
    depends on how Dispatch was installed. In order of trust:
      1. `$DISPATCH_SKILL_DIR` — an explicit override.
      2. A source checkout above this file (editable / from-source installs).
      3. An installed Claude plugin bundle — if the user already has Dispatch in
         Claude Code, that copy is authoritative and matches their version.
    Returns None when only a network fetch could find it; the CLI handles that.
    """
    override = os.environ.get("DISPATCH_SKILL_DIR")
    if override:
        candidate = Path(override).expanduser()
        if (candidate / "SKILL.md").is_file():
            return candidate
        if (candidate / SKILL_NAME / "SKILL.md").is_file():
            return candidate / SKILL_NAME

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "skills" / SKILL_NAME
        if (candidate / "SKILL.md").is_file():
            return candidate

    plugins = Path.home() / ".claude" / "plugins"
    try:
        matches = sorted(plugins.glob(f"**/skills/{SKILL_NAME}/SKILL.md"))
    except OSError:
        matches = []
    if matches:
        return matches[0].parent

    return None


def install(*, force: bool = False, skill_text: Optional[str] = None) -> dict:
    """Wire Dispatch into this machine's Codex: MCP server entry + skill.

    Idempotent. An existing `[mcp_servers.dispatch]` is left alone unless
    `force`, in which case the block is replaced (after a `.bak` copy — this is
    the user's own config and we are editing it in place). `skill_text` lets the
    caller supply SKILL.md content when `find_skill_source()` came up empty;
    without either, the MCP server is still installed and the skill is reported
    as skipped, because the tools work with or without it.

    Returns a report dict: what was written, what already matched, what was
    skipped and why. Raises OSError if the config itself can't be written —
    that is the one failure the caller must not paper over."""
    report: dict[str, Any] = {
        "codex_home": str(codex_home()),
        "config_path": str(config_path()),
        "codex_on_path": shutil.which("codex") is not None,
    }

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # A config that exists but won't read is the one case we must not write
        # through: appending to "" would replace the whole file with our block.
        # UnicodeDecodeError is a ValueError, hence the explicit catch.
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise OSError(f"{path} exists but could not be read ({exc}) — "
                          "refusing to overwrite it") from exc
    else:
        existing = ""

    entry = server_entry()
    header = f"[mcp_servers.{SERVER_NAME}]"
    lines = existing.splitlines(keepends=True)
    bounds = _section_bounds(lines, header) if entry is not None else None

    if entry is not None and not force:
        report["mcp_server"] = "already_present"
        report["mcp_server_command"] = entry.get("command")
    elif entry is not None and bounds is None:
        # The server is configured, but not as a `[mcp_servers.dispatch]`
        # table — someone declared it inline (`dispatch = { command = … }`
        # under `[mcp_servers]`). Appending our table would duplicate the key
        # and leave Codex unable to parse its own config at all, so refuse and
        # say where to look. The skill install below still runs.
        report["mcp_server"] = "manual_edit_needed"
        report["mcp_server_command"] = entry.get("command")
        report["mcp_server_error"] = (
            f"a dispatch MCP server is already configured in {path}, but not as a "
            f"'{header}' table (it looks inline). Edit it by hand so it reads: "
            f"command = \"{SERVER_COMMAND}\", startup_timeout_sec = "
            f"{STARTUP_TIMEOUT_S}, tool_timeout_sec = {TOOL_TIMEOUT_S}."
        )
    else:
        if bounds is not None:
            # Replacing our own block on --force: keep a copy of what was there
            # before we touch it.
            try:
                path.with_suffix(path.suffix + ".bak").write_text(existing, encoding="utf-8")
                report["backup"] = str(path.with_suffix(path.suffix + ".bak"))
            except OSError:
                pass
            start, end = bounds
            body = "".join(lines[:start]) + render_mcp_block().lstrip("\n") + "".join(lines[end:])
            report["mcp_server"] = "replaced"
        else:
            body = existing
            if body and not body.endswith("\n"):
                body += "\n"
            # The block leads with a blank line to separate it from whatever it
            # follows; on a brand-new config there's nothing to separate from.
            body += render_mcp_block() if body else render_mcp_block().lstrip("\n")
            report["mcp_server"] = "written"
        path.write_text(body, encoding="utf-8")
        report["mcp_server_command"] = SERVER_COMMAND

    # The skill: instructions only. Its absence costs natural-language triggers
    # ("dispatch this to Edward"), not the tools themselves.
    dest = skills_dir() / SKILL_NAME
    source = find_skill_source()
    if source is not None:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                target = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            report["skill"] = "copied"
            report["skill_source"] = str(source)
        except OSError as exc:
            report["skill"] = "failed"
            report["skill_error"] = str(exc)
    elif skill_text:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(skill_text, encoding="utf-8")
            report["skill"] = "fetched"
        except OSError as exc:
            report["skill"] = "failed"
            report["skill_error"] = str(exc)
    else:
        report["skill"] = "skipped"
        report["skill_error"] = (
            "could not find skills/dispatch/SKILL.md locally and no copy was "
            "supplied — the MCP tools work without it; install the Codex plugin "
            "or set $DISPATCH_SKILL_DIR to get the natural-language triggers"
        )
    report["skill_path"] = str(dest)
    return report


def status() -> dict:
    """Is Dispatch wired into this machine's Codex? Shaped for `doctor`."""
    entry = server_entry()
    skill = skills_dir() / SKILL_NAME / "SKILL.md"
    return {
        "codex_on_path": shutil.which("codex") is not None,
        "config_path": str(config_path()),
        "config_exists": config_path().exists(),
        "mcp_server_installed": entry is not None,
        "mcp_server_command": (entry or {}).get("command"),
        "skill_installed": skill.is_file(),
        "skill_path": str(skill.parent),
        "discovered_mcp_servers": sorted(discover_codex_mcp()),
    }
