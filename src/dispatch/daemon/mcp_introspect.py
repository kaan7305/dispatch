"""Enumerate the tools an installed MCP server exposes.

The edit-permissions dialog lets the recipient expand a granted MCP server and
tick *individual* tools (`mcp__<server>__<tool>` grants) rather than the whole
server. To render those checkboxes we need the server's actual tool list, which
lives nowhere in our config — only inside the server itself. So we do a real,
short-lived MCP handshake (`initialize` + `tools/list`) against each server's
own config and return just the tool names + descriptions.

This is best-effort by design. Some servers need OAuth (e.g. notion), a network
round-trip, or a process spawn that can fail or hang. Any failure raises
`McpIntrospectError` with a human reason; the caller degrades to the
whole-server checkbox rather than breaking the dialog. Nothing here ever widens
a grant — it only *reads* what a server offers.

Results are cached per (name, config-fingerprint) for a few minutes: tool lists
are near-static and a handshake is expensive (a process launch or HTTP session),
so re-enumerating on every dialog expand would be wasteful.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("dispatch.daemon.mcp_introspect")

# How long a single server handshake may take before we give up. Kept short so a
# wedged server can't hang the dialog; a slow-but-real server just degrades to
# the whole-server checkbox and the user can retry.
HANDSHAKE_TIMEOUT_S = 8.0

# Tool lists barely change; a handshake is costly. Cache successful enumerations
# briefly so expanding/collapsing a row doesn't re-spawn the server each time.
_CACHE_TTL_S = 300.0
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


class McpIntrospectError(Exception):
    """Enumeration failed for a reason worth showing the user (auth needed,
    timeout, transport error, malformed config)."""


def _fingerprint(config: dict[str, Any]) -> str:
    """Stable key for the cache that changes when the server's config does, so
    an edited command/url/env re-enumerates instead of serving stale tools."""
    try:
        return json.dumps(config, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted(config.items()))


def _transport_of(config: dict[str, Any]) -> str:
    """stdio | http | sse — mirror how the SDK picks a transport from a
    .mcp.json entry: an explicit `type` wins, else `command` ⇒ stdio and `url`
    ⇒ streamable http."""
    declared = str(config.get("type") or "").strip().lower()
    if declared in {"stdio", "http", "sse"}:
        return declared
    if declared in {"streamable-http", "streamable_http", "streamablehttp"}:
        return "http"
    if config.get("command"):
        return "stdio"
    if config.get("url"):
        return "sse" if declared == "sse" else "http"
    raise McpIntrospectError("unrecognized MCP server config (no command or url)")


def _humanize(exc: BaseException) -> str:
    """Best human-readable reason from a (possibly nested) handshake failure.

    Transport errors surface as ExceptionGroups whose leaves carry the real
    cause (an HTTP 401 for an OAuth server like notion, a connect refusal when
    offline, a missing binary for a stdio server). Walk to the most specific
    leaf and prefer an auth hint when we see one."""
    # Unwrap ExceptionGroup → most informative leaf.
    leaves: list[BaseException] = []

    def _walk(e: BaseException) -> None:
        inner = getattr(e, "exceptions", None)
        if inner:
            for sub in inner:
                _walk(sub)
        else:
            leaves.append(e)

    _walk(exc)
    for leaf in leaves:
        text = str(leaf)
        if "401" in text or "403" in text or "unauthor" in text.lower() or "auth" in text.lower():
            return "needs authentication (this server's tools aren't enumerable from the daemon)"
    leaf = leaves[-1] if leaves else exc
    msg = str(leaf).strip()
    return msg or type(leaf).__name__


async def _list_via_session(read, write) -> list[dict[str, str]]:
    from mcp import ClientSession

    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()
    return [
        {"name": t.name, "description": (t.description or "").strip()}
        for t in result.tools
    ]


async def _enumerate(name: str, config: dict[str, Any]) -> list[dict[str, str]]:
    transport = _transport_of(config)

    if transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client, get_default_environment

        command = config.get("command")
        if not command:
            raise McpIntrospectError("stdio server config missing 'command'")
        # Merge the server's declared env over the SDK's safe default set so a
        # server that relies on PATH/HOME still launches, exactly as it would
        # under the agent — without leaking the daemon's full environment.
        env = {**get_default_environment(), **(config.get("env") or {})}
        params = StdioServerParameters(
            command=command,
            args=list(config.get("args") or []),
            env=env,
            cwd=config.get("cwd"),
        )
        async with stdio_client(params) as (read, write):
            return await _list_via_session(read, write)

    url = config.get("url")
    if not url:
        raise McpIntrospectError(f"{transport} server config missing 'url'")
    headers = config.get("headers") or None

    if transport == "sse":
        from mcp.client.sse import sse_client

        async with sse_client(url, headers=headers) as (read, write):
            return await _list_via_session(read, write)

    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        return await _list_via_session(read, write)


async def list_server_tools(
    name: str,
    config: dict[str, Any],
    *,
    use_cache: bool = True,
) -> list[dict[str, str]]:
    """Return ``[{"name", "description"}, ...]`` for the server's tools.

    Raises ``McpIntrospectError`` (with a human-readable reason) on any handshake
    failure so the caller can fall back to a whole-server grant. Successful
    results are cached briefly per config fingerprint.
    """
    cache_key = f"{name}\0{_fingerprint(config)}"
    if use_cache:
        hit = _cache.get(cache_key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_S:
            return hit[1]

    try:
        tools = await asyncio.wait_for(
            _enumerate(name, config), timeout=HANDSHAKE_TIMEOUT_S
        )
    except McpIntrospectError:
        raise
    except asyncio.TimeoutError as exc:
        raise McpIntrospectError(
            f"timed out after {HANDSHAKE_TIMEOUT_S:.0f}s connecting to '{name}'"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — incl. ExceptionGroup; one reason out
        logger.info("MCP tool enumeration for %r failed: %r", name, exc)
        raise McpIntrospectError(_humanize(exc)) from exc

    tools.sort(key=lambda t: t["name"])
    _cache[cache_key] = (time.monotonic(), tools)
    return tools


def invalidate(name: Optional[str] = None) -> None:
    """Drop cached tool lists — all of them, or just one server's."""
    if name is None:
        _cache.clear()
        return
    for key in [k for k in _cache if k.split("\0", 1)[0] == name]:
        _cache.pop(key, None)
