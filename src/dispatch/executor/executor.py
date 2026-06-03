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

# Every built-in tool the agent could have. Anything not in the caller's
# `allowed_tools` is sent to the SDK as a disallowed tool — the agent
# cannot use it at all.
ALL_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
TOOL_RESULT_TRUNCATE_BYTES = 8 * 1024

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
    mcp_servers: dict[str, Any] | None = None,
    skills: list[str] | str | None = None,
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
    workflow attaching context-specific instructions). `mcp_servers` exposes
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
    """
    in_scope = list(allowed_tools) if allowed_tools is not None else list(ALL_TOOLS)
    disallowed = [t for t in ALL_TOOLS if t not in in_scope]

    if can_use_tool is not None:
        sdk_allowed_tools: list[str] = []   # nothing auto-approved
        permission_mode = "default"
    else:
        sdk_allowed_tools = in_scope
        permission_mode = "acceptEdits"

    options_kwargs: dict[str, Any] = {
        "allowed_tools": sdk_allowed_tools,
        "disallowed_tools": disallowed,
        "permission_mode": permission_mode,
        "cwd": cwd,
        "can_use_tool": can_use_tool,
        # Default framing so the task is understood as work to perform, not a
        # message to relay. Overridable by the caller (workflows).
        "system_prompt": system_prompt or DELEGATED_TASK_SYSTEM_PROMPT,
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

    async with ClaudeSDKClient(options=options) as client:
        try:
            await client.query(payload.task)
            async for message in client.receive_response():
                async for event in _normalize(message):
                    yield event
        except Exception as exc:
            yield {
                "type": "error",
                "data": {"message": str(exc), "exception": type(exc).__name__},
            }
            raise
