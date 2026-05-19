"""Dispatch executor.

Transport-agnostic. Takes a DispatchPayload, opens a Claude Agent SDK
session, runs the task, and yields normalized DispatchEvents. Knows
nothing about HTTP, WebSockets, or the frontend — the same generator
will be called by the future recipient daemon.

Permission policy ("which tools require human consent?") is NOT decided
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

# Tools auto-approved when no can_use_tool callback is provided.
DEFAULT_ALLOWED_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
# Subset auto-approved when a callback IS provided. The callback gates
# everything outside this list (Write, Edit, Bash by default).
READ_ONLY_ALLOWED_TOOLS: list[str] = ["Read", "Glob", "Grep"]
TOOL_RESULT_TRUNCATE_BYTES = 8 * 1024


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
    can_use_tool: CanUseTool | None = None,
) -> AsyncIterator[DispatchEvent]:
    # `allowed_tools` maps to --allowedTools, which AUTO-APPROVES the
    # named tools and bypasses can_use_tool. So when a callback is
    # provided we pre-approve only read-only tools and let the callback
    # gate the rest. Without a callback we fall back to acceptEdits and
    # auto-approve the full set so the generator is still usable from a
    # script.
    if can_use_tool is not None:
        allowed = READ_ONLY_ALLOWED_TOOLS
        permission_mode = "default"
    else:
        allowed = DEFAULT_ALLOWED_TOOLS
        permission_mode = "acceptEdits"

    options = ClaudeAgentOptions(
        allowed_tools=allowed,
        permission_mode=permission_mode,
        cwd=cwd,
        can_use_tool=can_use_tool,
    )

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
