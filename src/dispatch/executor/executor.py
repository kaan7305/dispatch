"""Dispatch executor.

Transport-agnostic. Takes a DispatchPayload, opens a Claude Agent SDK
session, runs the task, and yields normalized DispatchEvents. Knows
nothing about HTTP, WebSockets, or the frontend — the same generator
will be called by the future recipient daemon.

Permission policy ("which tools require human approval?") is NOT decided
here. The caller injects a `can_use_tool` callback if it wants to gate
tool execution. The executor just wires it through to the SDK.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from dispatch.shared.schema import DispatchEvent, DispatchPayload
from dispatch.shared.signing import canonical_context

# Every built-in tool the agent could have. Anything not in the caller's
# `allowed_tools` is sent to the SDK as a disallowed tool — the agent
# cannot use it at all.
ALL_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
TOOL_RESULT_TRUNCATE_BYTES = 8 * 1024

# Model the delegated-task agent runs on. Defaults to Sonnet — a delegated
# chore ("send this email", "create a folder") doesn't need Opus, and the SDK
# would otherwise inherit the caller's top-tier default, making every dispatch
# 5–15× more expensive than it needs to be. Override per-deployment with
# $DISPATCH_EXECUTOR_MODEL (e.g. a full id, or "haiku" for the cheapest tier).
DEFAULT_EXECUTOR_MODEL = os.environ.get("DISPATCH_EXECUTOR_MODEL") or "claude-sonnet-4-6"

# MCP connection statuses (from the CLI init event) that mean the server will
# NOT provide tools this run. "pending" is excluded — it may still finish
# connecting; "connected" is the only healthy state.
_DEAD_MCP_STATUSES = {"failed", "needs-auth", "disabled"}


def _dead_scoped_mcp(init_data: dict, expected: set[str]) -> list[tuple[str, str]]:
    """Given an init SystemMessage's data and the MCP servers the task was
    scoped to use, return the (name, status) of those that won't come up —
    failed/needs-auth/disabled, or absent from the report entirely. A server
    still "pending" gets the benefit of the doubt and is not reported dead."""
    reported: dict[str, str] = {}
    for srv in (init_data.get("mcp_servers") or []):
        if isinstance(srv, dict) and srv.get("name"):
            reported[srv["name"]] = str(srv.get("status", "")).lower()
    dead: list[tuple[str, str]] = []
    for name in sorted(expected):
        status = reported.get(name)
        if status is None:
            dead.append((name, "missing"))
        elif status in _DEAD_MCP_STATUSES:
            dead.append((name, status))
    return dead


# Framing prepended to every delegated task so the agent understands the
# message is a task someone else handed it to *run*, not an instruction to
# relay. This is for disambiguation only (a task like "create a folder on
# edward's desktop" shouldn't be read as "send this to edward") — it is NOT a
# security control: the inability to dispatch onward is enforced by withholding
# the dispatch tools in the caller's `can_use_tool`, not by this text.
DELEGATED_TASK_SYSTEM_PROMPT = (
    "You are carrying out a task that another person delegated to your agent "
    "over Dispatch. The user message is that task — execute it directly with "
    "your available tools in the working directory. Someone else wrote it for "
    "you to perform: treat names, paths, and phrases like 'to <name>' as part "
    "of the task itself (e.g. a destination folder), never as an instruction to "
    "send, forward, relay, or re-dispatch anything to anyone."
)


def _scope_notice(in_scope: list[str], disallowed: list[str]) -> str:
    """A hard statement of the agent's tool scope, appended to the system prompt
    so the agent knows its limits UP FRONT instead of discovering them by trying
    a tool and getting denied. The reported bug: with a read-only edge the agent
    would attempt Write/Bash, get denied, and keep hunting for a workaround. This
    tells it the boundary and that there is no way around it — if the task needs
    a tool it doesn't have, stop and report rather than iterate."""
    allowed = ", ".join(in_scope) if in_scope else "(none)"
    notice = (
        f"\n\nTOOL SCOPE (hard limit set by the person whose machine you run on): "
        f"you have ONLY these tools — {allowed}."
    )
    if disallowed:
        notice += (
            f" These tools are NOT granted and every attempt to use them WILL be "
            f"denied — there is no workaround: {', '.join(disallowed)}."
        )
    notice += (
        " If the task needs a capability you don't have (e.g. it asks you to "
        "create or modify files but you lack Write/Edit, or to run a command but "
        "you lack Bash), do NOT retry the denied tool, look for an alternate "
        "route, or improvise around it. Stop immediately and reply in plain "
        "language stating exactly which tool/capability the task requires and "
        "that your trust scope doesn't grant it, so the sender can ask the "
        "recipient to widen the scope. Reporting that cleanly is the correct, "
        "successful outcome — not a failure to keep trying."
    )
    return notice


def _rich_payload_trailer(
    payload: DispatchPayload, attachment_paths: list[str] | None
) -> str:
    """Sender-authored context + attachment locations, appended to the task
    query. Everything here is covered by the dispatch signature (context
    directly; attachments via the signed manifest the daemon verified), so
    it carries the same trust as the task text itself."""
    parts: list[str] = []
    ctx = canonical_context(payload.metadata) or {}
    if ctx:
        lines = ["--- Context from the sender ---"]
        if ctx.get("project"):
            lines.append(f"Project: {ctx['project']}")
        if ctx.get("deliverable"):
            lines.append(f"Expected deliverable: {ctx['deliverable']}")
        if ctx.get("links"):
            lines.append("Reference links:")
            lines.extend(f"  - {l}" for l in ctx["links"])
        if ctx.get("background"):
            lines.append(f"Background (sender's session summary):\n{ctx['background']}")
        parts.append("\n".join(lines))
    if attachment_paths:
        lines = [
            "--- Attached files (sent with this task, integrity-verified, "
            "already saved locally) ---"
        ]
        lines.extend(f"  {p}" for p in attachment_paths)
        parts.append("\n".join(lines))
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= TOOL_RESULT_TRUNCATE_BYTES:
        return text, False
    return text[:TOOL_RESULT_TRUNCATE_BYTES] + "\n... [truncated]", True


def _tool_result_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                parts.append(text if text else str(block))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


async def _normalize(message: Any) -> AsyncIterator[DispatchEvent]:
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                yield {"type": "agent_text", "data": {"text": block.text}}
            elif isinstance(block, ToolUseBlock):
                yield {
                    "type": "tool_use",
                    "data": {"id": block.id, "name": block.name, "input": block.input},
                }
    elif isinstance(message, UserMessage):
        content = message.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    text = _tool_result_to_text(block.content)
                    truncated_text, was_truncated = _truncate(text)
                    yield {
                        "type": "tool_result",
                        "data": {
                            "tool_use_id": block.tool_use_id,
                            "content": truncated_text,
                            "is_error": bool(block.is_error),
                            "truncated": was_truncated,
                        },
                    }
    elif isinstance(message, ResultMessage):
        yield {
            "type": "done",
            "data": {
                "subtype": message.subtype,
                "is_error": message.is_error,
                "duration_ms": message.duration_ms,
                "num_turns": message.num_turns,
                "total_cost_usd": message.total_cost_usd,
            },
        }
    elif isinstance(message, SystemMessage):
        return


async def run_dispatch(
    payload: DispatchPayload,
    *,
    cwd: str | Path | None = None,
    allowed_tools: list[str] | None = None,
    can_use_tool: CanUseTool | None = None,
    system_prompt: str | None = None,
    extra_system_prompt: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
    skills: list[str] | str | None = None,
    model: str | None = None,
    attachment_paths: list[str] | None = None,
) -> AsyncIterator[DispatchEvent]:
    """Run one dispatch.

    `allowed_tools` is the set of tools the agent may use at all (the
    trust edge's scope). Anything outside it is sent as a disallowed
    tool so the agent can't even attempt it.

    With `can_use_tool` set, NO tool is auto-approved — every in-scope
    call is routed through the callback, which the caller uses to enforce
    path scope and the manual/auto approval policy. Without a callback
    (script use) the in-scope tools are auto-accepted.

    `system_prompt` overrides the default delegated-task framing (e.g. a
    workflow attaching context-specific instructions); `extra_system_prompt`
    is appended after the framing + scope notice — advisory context like
    learned machine memory, which must never displace the framing or the
    scope notice. `mcp_servers` exposes
    the recipient's own MCP servers to the task so a dispatch can use the
    recipient's powerful tools; the caller's `can_use_tool` still gates every
    call against the edge scope and withholds the dispatch control plane.

    `skills` enables the recipient's Skills for the task — pass "all" to expose
    every installed Skill (or a list of names). Skills are just instructions and
    grant NO capability on their own: a Skill that wants to run a script still
    needs `Bash`, an API still needs that MCP tool, all gated by `can_use_tool`.
    So there's nothing to sandbox at the skill level — the tools are the
    boundary. Enabling skills flips `setting_sources` to ["user"] so the SDK can
    discover them (this also exposes the recipient's CLAUDE.md to the task);
    `strict_mcp_config` still keeps MCP explicit and the dispatch control plane
    is still withheld in `can_use_tool`. Omit/None → no skills (isolated base).

    `attachment_paths` are files the daemon already materialized from the
    dispatch's verified attachments; their locations (plus any sender context
    in metadata) are appended to the task query as a trailer.
    """
    in_scope = list(allowed_tools) if allowed_tools is not None else list(ALL_TOOLS)
    disallowed = [t for t in ALL_TOOLS if t not in in_scope]

    # Whatever framing we use (default or a workflow override), always state the
    # tool scope explicitly so the agent knows its limits before it acts and
    # won't burn turns probing for a way around a tool it wasn't granted.
    # `extra_system_prompt` is appended on top — advisory context (e.g. learned
    # machine memory) that supplements the framing instead of replacing it.
    effective_system_prompt = (
        (system_prompt or DELEGATED_TASK_SYSTEM_PROMPT) + _scope_notice(in_scope, disallowed)
    )
    if extra_system_prompt:
        effective_system_prompt += "\n\n" + extra_system_prompt

    if can_use_tool is not None:
        sdk_allowed_tools: list[str] = []   # nothing auto-approved
        permission_mode = "default"
    else:
        sdk_allowed_tools = in_scope
        permission_mode = "acceptEdits"

    options_kwargs: dict[str, Any] = {
        "model": model or DEFAULT_EXECUTOR_MODEL,
        "allowed_tools": sdk_allowed_tools,
        "disallowed_tools": disallowed,
        "permission_mode": permission_mode,
        "cwd": cwd,
        "can_use_tool": can_use_tool,
        # Default framing so the task is understood as work to perform, not a
        # message to relay (caller-overridable), plus an explicit tool-scope
        # notice appended so the agent knows its hard limits up front.
        "system_prompt": effective_system_prompt,
        # Clean base: do NOT inherit the recipient's settings (their plugins,
        # skills, or filesystem MCP config). This is what makes the
        # dispatch-control exclusion airtight — the dispatch plugin can't even
        # load into a delegated task — and keeps the task least-privilege. The
        # only MCP servers a task gets are the ones explicitly passed below,
        # gated per-call by can_use_tool.
        "setting_sources": [],
        "strict_mcp_config": True,
    }
    if mcp_servers:
        options_kwargs["mcp_servers"] = mcp_servers
    if skills:
        # Discover the recipient's Skills (and CLAUDE.md). Capability stays
        # bounded: strict_mcp_config keeps MCP explicit and can_use_tool gates
        # every tool, so skills add nothing the edge didn't already grant.
        options_kwargs["setting_sources"] = ["user"]
        options_kwargs["skills"] = skills
    options = ClaudeAgentOptions(**options_kwargs)

    expected_mcp = set((mcp_servers or {}).keys())

    # The query is the signed task plus any rich-payload trailer (sender
    # context + verified attachment locations) — same author, same trust.
    query_text = payload.task + _rich_payload_trailer(payload, attachment_paths)

    async with ClaudeSDKClient(options=options) as client:
        try:
            await client.query(query_text)
            mcp_checked = not expected_mcp  # nothing scoped → nothing to check
            async for message in client.receive_response():
                # Fast-fail: the CLI's init event reports each MCP server's
                # connection status. If a server the edge scoped didn't come
                # up, the task was granted a capability it can't use — surface
                # it to the sender and abort, instead of letting the agent burn
                # turns (and tokens) retrying tools that will never appear.
                if (not mcp_checked and isinstance(message, SystemMessage)
                        and message.subtype == "init"):
                    mcp_checked = True
                    dead = _dead_scoped_mcp(message.data, expected_mcp)
                    if dead:
                        listed = ", ".join(f"{name} ({status})" for name, status in dead)
                        yield {
                            "type": "error",
                            "data": {
                                "message": (
                                    "MCP server(s) the task was scoped to use are "
                                    f"unavailable: {listed}. Aborting before the agent "
                                    "spends turns retrying tools that will never appear — "
                                    "check the server's command/auth on the recipient "
                                    "machine."
                                ),
                                "exception": "McpServerUnavailable",
                                "servers": [name for name, _ in dead],
                            },
                        }
                        return  # exits the `async with` → terminates the SDK session
                async for event in _normalize(message):
                    yield event
        except Exception as exc:
            yield {
                "type": "error",
                "data": {"message": str(exc), "exception": type(exc).__name__},
            }
            raise
