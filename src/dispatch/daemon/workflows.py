"""Daemon-side workflow executor.

The workflow IS the dispatch payload — the sender designs a graph and
fans it out to N recipients. Each recipient's daemon, on accepting the
dispatch, runs the engine here. There is no `dispatch` node — dispatch
is the delivery, not a node type. Node executors are all local:
agent (Claude prompt), notify, branch, transform.code, http.request,
delay, end.success, end.error, plus the two trigger types that just
pass `input` through.

Flow:
  1. The broker creates one dispatch per recipient via POST
     /workflows/{id}/run; each dispatch payload carries a
     WorkflowDispatchEnvelope in metadata.workflow with a pre-allocated
     run_id.
  2. The recipient daemon's process_dispatch detects metadata.workflow,
     and instead of run_dispatch() calls engine.run_for_dispatch(...).
  3. run_for_dispatch walks the graph, PATCHing /runs/{id} after every
     node-state change. Final node output becomes an agent_text event so
     the sender's SPA sees a result on the dispatch detail page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

import httpx

from dispatch.executor import run_dispatch
from dispatch.shared.schema import (
    DispatchPayload,
    NodeState,
    NodeStatus,
    WorkflowRunStatus,
)

logger = logging.getLogger("dispatch.daemon.workflows")

# {{nN.output}} and {{ctx.key}} — single braces are NOT recognised so
# JSON example bodies in node params don't get accidentally substituted.
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")

# Sentinel used in keyword args where None is a legitimate value.
_MISSING = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowEngine:
    def __init__(
        self,
        local_state,
        broker_url: str,
        broker_token: str,
        http_client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
    ) -> None:
        self.local_state = local_state
        self.broker_url = broker_url.rstrip("/")
        self.broker_token = broker_token
        # run_id -> task. process_dispatch awaits the engine inline so the
        # task here is more of a handle for cancel() than a fire-and-forget.
        self.runs: dict[UUID, asyncio.Task] = {}
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=10.0)
        )

    def cancel(self, run_id: UUID) -> bool:
        task = self.runs.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def run_for_dispatch(
        self,
        *,
        run_id: UUID,
        definition: dict,
        input_: dict,
        workspace,
        allowed_tools: list[str],
        can_use_tool,
        sender_id: str,
        send_event,
    ) -> WorkflowRunStatus:
        """Execute a workflow that arrived as a dispatch payload.

        Returns the terminal WorkflowRunStatus so the caller can map it to
        a DispatchStatus and call send_status. The engine itself does NOT
        touch dispatch status — that stays the caller's concern.

        `send_event` is the per-dispatch send_event used by process_dispatch
        so node outputs surface as agent_text in the sender's watch view.
        """
        task = asyncio.create_task(
            self._execute(
                run_id=run_id,
                definition=definition,
                input_=input_,
                workspace=workspace,
                allowed_tools=allowed_tools,
                can_use_tool=can_use_tool,
                sender_id=sender_id,
                send_event=send_event,
            )
        )
        self.runs[run_id] = task
        try:
            return await task
        finally:
            self.runs.pop(run_id, None)

    async def _execute(
        self,
        *,
        run_id: UUID,
        definition: dict,
        input_: dict,
        workspace,
        allowed_tools: list[str],
        can_use_tool,
        sender_id: str,
        send_event,
    ) -> WorkflowRunStatus:
        nodes = definition.get("nodes", []) or []
        edges = definition.get("edges", []) or []
        nodes_by_id: dict[str, dict] = {n["id"]: n for n in nodes}

        node_states: dict[str, dict] = {
            n["id"]: NodeState(status=NodeStatus.pending).model_dump(mode="json")
            for n in nodes
        }

        try:
            order = _topo_sort(nodes, edges)
        except _WorkflowGraphError as exc:
            await self._patch_run(
                run_id,
                status=WorkflowRunStatus.failed,
                node_states=node_states,
                error=str(exc),
                ended=True,
            )
            return WorkflowRunStatus.failed

        triggers = [n for n in nodes if str(n.get("type", "")).startswith("trigger.")]
        if len(triggers) != 1:
            await self._patch_run(
                run_id,
                status=WorkflowRunStatus.failed,
                node_states=node_states,
                error=f"expected exactly one trigger.* node, found {len(triggers)}",
                ended=True,
            )
            return WorkflowRunStatus.failed

        # Branch nodes gate their outgoing edges. A non-trigger node only
        # runs if at least one of its incoming edges is "alive" — not
        # skipped and not on the untaken side of a branch.
        taken_ports: set[tuple[str, str]] = set()
        skipped_nodes: set[str] = set()

        def is_edge_alive(edge: dict) -> bool:
            src = edge.get("from")
            sport = edge.get("from_port", "out")
            if src in skipped_nodes:
                return False
            src_node = nodes_by_id.get(src)
            if src_node and src_node.get("type") == "branch":
                return (src, sport) in taken_ports
            return True

        try:
            await self._patch_run(run_id, status=WorkflowRunStatus.running)

            for node_id in order:
                node = nodes_by_id[node_id]
                ntype = node.get("type", "")
                incoming = [e for e in edges if e.get("to") == node_id]

                if not ntype.startswith("trigger."):
                    if not incoming or not any(is_edge_alive(e) for e in incoming):
                        skipped_nodes.add(node_id)
                        await self._set_node_state(
                            run_id, node_states, node_id,
                            status=NodeStatus.skipped,
                            ended_at=_utcnow(),
                        )
                        continue

                await self._set_node_state(
                    run_id, node_states, node_id,
                    status=NodeStatus.running,
                    started_at=_utcnow(),
                )

                try:
                    if ntype == "trigger.manual" or ntype == "trigger.cron":
                        output: Any = input_
                    elif ntype == "agent":
                        output = await self._run_agent_node(
                            node, node_states, input_,
                            workspace=workspace,
                            allowed_tools=allowed_tools,
                            can_use_tool=can_use_tool,
                            sender_id=sender_id,
                            send_event=send_event,
                        )
                    elif ntype == "branch":
                        branch_result = self._run_branch_node(node, node_states, input_)
                        taken_ports.add((node_id, branch_result["port"]))
                        output = branch_result["value"]
                    elif ntype == "switch":
                        switch_result = self._run_switch_node(node, node_states, input_)
                        taken_ports.add((node_id, switch_result["port"]))
                        output = switch_result["value"]
                    elif ntype == "filter":
                        filter_result = self._run_filter_node(node, node_states, input_)
                        if not filter_result["pass"]:
                            # Skip every downstream by claiming "out" is
                            # not taken — same gating model as a branch
                            # that never selected this port.
                            skipped_nodes.add(node_id)
                            await self._set_node_state(
                                run_id, node_states, node_id,
                                status=NodeStatus.completed,
                                output=filter_result,
                                ended_at=_utcnow(),
                            )
                            continue
                        output = filter_result
                    elif ntype == "merge":
                        live_incoming = [e["from"] for e in incoming if is_edge_alive(e)]
                        output = self._run_merge_node(
                            node, node_states, input_, live_incoming,
                        )
                    elif ntype == "notify":
                        output = self._run_notify_node(node, node_states, input_)
                    elif ntype == "notify.sound":
                        output = await self._run_sound_node(node, node_states, input_)
                    elif ntype == "log":
                        output = self._run_log_node(node, node_states, input_)
                    elif ntype == "set":
                        output = self._run_set_node(node, node_states, input_)
                    elif ntype == "format":
                        output = self._run_format_node(node, node_states, input_)
                    elif ntype == "random":
                        output = self._run_random_node(node, node_states, input_)
                    elif ntype == "math":
                        output = self._run_math_node(node, node_states, input_)
                    elif ntype == "string":
                        output = self._run_string_node(node, node_states, input_)
                    elif ntype == "regex":
                        output = self._run_regex_node(node, node_states, input_)
                    elif ntype == "json.parse":
                        output = self._run_json_parse_node(node, node_states, input_)
                    elif ntype == "json.stringify":
                        output = self._run_json_stringify_node(node, node_states, input_)
                    elif ntype == "hash":
                        output = self._run_hash_node(node, node_states, input_)
                    elif ntype == "base64":
                        output = self._run_base64_node(node, node_states, input_)
                    elif ntype == "datetime":
                        output = self._run_datetime_node(node, node_states, input_)
                    elif ntype == "file.read":
                        output = self._run_file_read_node(node, node_states, input_, workspace)
                    elif ntype == "file.write":
                        output = self._run_file_write_node(node, node_states, input_, workspace)
                    elif ntype == "context":
                        output = self._run_context_node(node, node_states, input_, workspace)
                    elif ntype == "ai.classify":
                        output = await self._run_ai_classify_node(
                            node, node_states, input_,
                            workspace=workspace, allowed_tools=allowed_tools,
                            can_use_tool=can_use_tool, sender_id=sender_id,
                            send_event=send_event,
                        )
                    elif ntype == "ai.extract":
                        output = await self._run_ai_extract_node(
                            node, node_states, input_,
                            workspace=workspace, allowed_tools=allowed_tools,
                            can_use_tool=can_use_tool, sender_id=sender_id,
                            send_event=send_event,
                        )
                    elif ntype == "ai.summarize":
                        output = await self._run_ai_summarize_node(
                            node, node_states, input_,
                            workspace=workspace, allowed_tools=allowed_tools,
                            can_use_tool=can_use_tool, sender_id=sender_id,
                            send_event=send_event,
                        )
                    elif ntype == "ai.judge":
                        output = await self._run_ai_judge_node(
                            node, node_states, input_,
                            workspace=workspace, allowed_tools=allowed_tools,
                            can_use_tool=can_use_tool, sender_id=sender_id,
                            send_event=send_event,
                        )
                    elif ntype == "transform.code":
                        output = self._run_code_node(node, node_states, input_)
                    elif ntype == "http.request":
                        output = await self._run_http_node(node, node_states, input_)
                    elif ntype == "delay":
                        output = await self._run_delay_node(node, node_states, input_)
                    elif ntype == "wait_until":
                        output = await self._run_wait_until_node(node, node_states, input_)
                    elif ntype == "end.success":
                        output = self._run_end_success_node(node, node_states, input_)
                    elif ntype == "end.error":
                        output = self._run_end_error_node(node, node_states, input_)
                    else:
                        raise _NodeError(f"unknown node type: {ntype!r}")
                except _NodeError as exc:
                    await self._set_node_state(
                        run_id, node_states, node_id,
                        status=NodeStatus.failed,
                        ended_at=_utcnow(),
                        error=str(exc),
                    )
                    await self._patch_run(
                        run_id,
                        status=WorkflowRunStatus.failed,
                        node_states=node_states,
                        error=f"node {node_id}: {exc}",
                        ended=True,
                    )
                    return WorkflowRunStatus.failed
                except _EndRun as exc:
                    await self._set_node_state(
                        run_id, node_states, node_id,
                        status=NodeStatus.completed,
                        output={"message": exc.message},
                        ended_at=_utcnow(),
                    )
                    await self._patch_run(
                        run_id,
                        status=exc.status,
                        node_states=node_states,
                        error=exc.message if exc.status == WorkflowRunStatus.failed else None,
                        ended=True,
                    )
                    return exc.status

                await self._set_node_state(
                    run_id, node_states, node_id,
                    status=NodeStatus.completed,
                    output=output,
                    ended_at=_utcnow(),
                )
        except asyncio.CancelledError:
            await self._patch_run(
                run_id,
                status=WorkflowRunStatus.cancelled,
                node_states=node_states,
                ended=True,
            )
            raise
        except Exception as exc:
            logger.exception("workflow engine crashed mid-run")
            await self._patch_run(
                run_id,
                status=WorkflowRunStatus.failed,
                node_states=node_states,
                error=f"engine error: {exc}",
                ended=True,
            )
            return WorkflowRunStatus.failed

        await self._patch_run(
            run_id,
            status=WorkflowRunStatus.completed,
            node_states=node_states,
            ended=True,
        )
        return WorkflowRunStatus.completed

    # ────────────────────────────────────────────────────────────────────
    # Per-node executors
    # ────────────────────────────────────────────────────────────────────

    async def _run_agent_node(
        self,
        node: dict,
        node_states: dict[str, dict],
        input_: dict,
        *,
        workspace,
        allowed_tools: list[str],
        can_use_tool,
        sender_id: str,
        send_event,
    ) -> str:
        params = node.get("params", {}) or {}
        prompt_tpl = params.get("prompt")
        if not prompt_tpl:
            raise _NodeError("agent node missing prompt")
        prompt = _hydrate(str(prompt_tpl), node_states, input_)
        sys_tpl = params.get("system_prompt")
        system_prompt = (
            _hydrate(str(sys_tpl), node_states, input_) if sys_tpl else None
        )
        return await self._invoke_llm(
            prompt,
            workspace=workspace,
            allowed_tools=allowed_tools,
            can_use_tool=can_use_tool,
            sender_id=sender_id,
            send_event=send_event,
            system_prompt=system_prompt,
        )

    async def _invoke_llm(
        self,
        prompt: str,
        *,
        workspace,
        allowed_tools: list[str],
        can_use_tool,
        sender_id: str,
        send_event,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Shared Claude invocation used by `agent` and every `ai.*` node.

        Uses the same run_dispatch generator that powers single-prompt
        dispatches, with the parent dispatch's tool scopes + per-tool
        approval callback. Accumulates the assistant's `agent_text` into
        the return value and forwards every event up so the sender's SPA
        sees live thinking + tool use.
        """
        synthetic = DispatchPayload(
            sender_id=sender_id,
            recipient_id="local",
            task=prompt,
            expires_at=_utcnow(),
        )
        text_chunks: list[str] = []
        try:
            async for event in run_dispatch(
                synthetic,
                cwd=str(workspace),
                allowed_tools=list(allowed_tools),
                can_use_tool=can_use_tool,
                system_prompt=system_prompt,
            ):
                if event["type"] == "agent_text":
                    text_chunks.append(str(event.get("data", {}).get("text", "")))
                if event["type"] == "error":
                    await send_event(synthetic.dispatch_id, event)
                    raise _NodeError(
                        str(event.get("data", {}).get("message", "agent error"))
                    )
                await send_event(synthetic.dispatch_id, event)
        except _NodeError:
            raise
        except Exception as exc:
            raise _NodeError(f"llm invocation failed: {exc}")
        return "".join(text_chunks).strip()

    def _run_branch_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        left  = _hydrate(str(params.get("left", "")), node_states, input_).strip()
        right = _hydrate(str(params.get("right", "")), node_states, input_).strip()
        op    = str(params.get("op", "==")).strip()
        if op == "==":
            result = left == right
        elif op == "!=":
            result = left != right
        elif op == "contains":
            result = right in left
        else:
            raise _NodeError(f"branch op must be ==, !=, or contains (got {op!r})")
        return {
            "port": "out_true" if result else "out_false",
            "value": {"left": left, "right": right, "op": op, "result": result},
        }

    def _run_switch_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Multi-way branch. Params:
            value: template          (the discriminator)
            cases: [{when: str, port: str}]
            default_port: str        (port used when no case matches)
        The first case whose `when` equals the hydrated value wins.
        """
        params = node.get("params", {}) or {}
        value = _hydrate(str(params.get("value", "")), node_states, input_).strip()
        cases = params.get("cases") or []
        default_port = str(params.get("default_port", "out_default")) or "out_default"
        if not isinstance(cases, list):
            raise _NodeError("switch 'cases' must be a list")

        for case in cases:
            if not isinstance(case, dict):
                continue
            when = _hydrate(str(case.get("when", "")), node_states, input_).strip()
            port = str(case.get("port") or "").strip()
            if not port:
                continue
            if when == value:
                return {"port": port, "value": {"value": value, "matched": when}}
        return {"port": default_port, "value": {"value": value, "matched": None}}

    def _run_set_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Define a bag of variables for downstream nodes. Params:
            values: { key: template_string, ... }
        Output is a dict; downstream nodes read with {{thisId.output.key}}.
        """
        params = node.get("params", {}) or {}
        values = params.get("values") or {}
        if not isinstance(values, dict):
            raise _NodeError("set 'values' must be an object")
        out: dict[str, str] = {}
        for k, v in values.items():
            out[str(k)] = _hydrate(str(v), node_states, input_)
        return out

    def _run_format_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> str:
        """A typed convenience over a one-line transform.code: hydrate
        a single template and return the string."""
        params = node.get("params", {}) or {}
        template = str(params.get("template", ""))
        return _hydrate(template, node_states, input_)

    def _run_filter_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Gate downstream nodes. If the condition is true, output a
        passthrough dict and let downstream run; if false, the engine
        marks downstream as skipped (handled at the call site)."""
        params = node.get("params", {}) or {}
        left  = _hydrate(str(params.get("left", "")), node_states, input_).strip()
        right = _hydrate(str(params.get("right", "")), node_states, input_).strip()
        op    = str(params.get("op", "==")).strip()
        if op == "==":
            result = left == right
        elif op == "!=":
            result = left != right
        elif op == "contains":
            result = right in left
        elif op == "starts_with":
            result = left.startswith(right)
        elif op == "ends_with":
            result = left.endswith(right)
        elif op == "is_empty":
            result = left == ""
        elif op == "is_not_empty":
            result = left != ""
        else:
            raise _NodeError(f"filter op not supported: {op!r}")
        return {"pass": result, "left": left, "right": right, "op": op}

    def _run_merge_node(
        self,
        node: dict,
        node_states: dict[str, dict],
        input_: dict,
        incoming_source_ids: list[str],
    ) -> dict:
        """Collect outputs of every live incoming edge into one dict
        keyed by source node id. Useful as a join point after a fan-out."""
        out: dict[str, Any] = {}
        for src in incoming_source_ids:
            state = node_states.get(src) or {}
            if state.get("status") == NodeStatus.completed.value:
                out[src] = state.get("output")
        return out

    def _run_math_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        op = str(params.get("op", "+")).strip()
        try:
            left  = float(_hydrate(str(params.get("left", "0")),  node_states, input_).strip() or 0)
            right = float(_hydrate(str(params.get("right", "0")), node_states, input_).strip() or 0)
        except ValueError as exc:
            raise _NodeError(f"math: non-numeric operand: {exc}")
        if op == "+":
            value = left + right
        elif op == "-":
            value = left - right
        elif op == "*":
            value = left * right
        elif op == "/":
            if right == 0:
                raise _NodeError("math: division by zero")
            value = left / right
        elif op == "%":
            if right == 0:
                raise _NodeError("math: modulo by zero")
            value = left % right
        elif op == "**":
            value = left ** right
        elif op == "min":
            value = min(left, right)
        elif op == "max":
            value = max(left, right)
        else:
            raise _NodeError(f"math op not supported: {op!r}")
        # Return int if it's a whole number — avoids ugly 5.0 in templates.
        as_int = int(value)
        if as_int == value:
            value = as_int
        return {"value": value, "left": left, "right": right, "op": op}

    def _run_string_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        op = str(params.get("op", "trim")).strip()
        value = _hydrate(str(params.get("value", "")), node_states, input_)
        if op == "upper":
            result = value.upper()
        elif op == "lower":
            result = value.lower()
        elif op == "title":
            result = value.title()
        elif op == "trim":
            result = value.strip()
        elif op == "reverse":
            result = value[::-1]
        elif op == "length":
            return {"value": len(value), "op": op}
        elif op == "slice":
            try:
                start = int(params.get("start", 0))
                end_param = params.get("end")
                end = int(end_param) if end_param not in (None, "") else len(value)
            except (TypeError, ValueError):
                raise _NodeError("string slice needs integer start/end")
            result = value[start:end]
        elif op == "replace":
            find = _hydrate(str(params.get("find", "")), node_states, input_)
            repl = _hydrate(str(params.get("replace", "")), node_states, input_)
            result = value.replace(find, repl)
        elif op == "split":
            sep = str(params.get("separator", ","))
            return {"value": value.split(sep), "op": op}
        elif op == "join":
            sep = str(params.get("separator", ","))
            try:
                # Walk the template ref straight, supporting dict access.
                # When `value` is already a serialized list-ish string we
                # fall back to splitting on the separator.
                raw = params.get("value")
                if isinstance(raw, list):
                    result = sep.join(str(x) for x in raw)
                else:
                    result = sep.join(value.split(sep))
            except TypeError:
                raise _NodeError("string join: 'value' must hydrate to a list")
        else:
            raise _NodeError(f"string op not supported: {op!r}")
        return {"value": result, "op": op}

    def _run_regex_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        op = str(params.get("op", "extract")).strip()
        pattern = str(params.get("pattern", ""))
        value = _hydrate(str(params.get("value", "")), node_states, input_)
        if not pattern:
            raise _NodeError("regex requires 'pattern'")
        try:
            compiled = re.compile(pattern, re.DOTALL)
        except re.error as exc:
            raise _NodeError(f"regex compile failed: {exc}")
        if op == "extract":
            match = compiled.search(value)
            if not match:
                return {"matched": False, "groups": [], "value": None}
            return {
                "matched": True,
                "value": match.group(0),
                "groups": list(match.groups()),
                "named": match.groupdict(),
            }
        if op == "extract_all":
            matches = compiled.findall(value)
            return {"matched": bool(matches), "matches": matches}
        if op == "replace":
            repl = _hydrate(str(params.get("replace", "")), node_states, input_)
            return {"value": compiled.sub(repl, value)}
        if op == "test":
            return {"matched": bool(compiled.search(value))}
        raise _NodeError(f"regex op not supported: {op!r}")

    def _run_json_parse_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> Any:
        params = node.get("params", {}) or {}
        raw = _hydrate(str(params.get("value", "")), node_states, input_).strip()
        if not raw:
            raise _NodeError("json.parse: empty input")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _NodeError(f"json.parse failed: {exc}")

    def _run_json_stringify_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> str:
        """Serialise a referenced node output to JSON.

        `value` is interpreted as a node-state reference (e.g. "n2.output"
        or "n2.output.foo") so we can serialize a structured value
        instead of forcing it through string hydration first.
        """
        params = node.get("params", {}) or {}
        ref = str(params.get("value", "")).strip()
        pretty = bool(params.get("pretty", False))
        if not ref:
            raise _NodeError("json.stringify: 'value' (ref) is required")
        # Use _walk_path against either node_states or ctx.
        segments = ref.split(".")
        head = segments[0]
        if head == "ctx":
            target = _walk_path(input_, segments[1:])
        elif head in node_states and len(segments) >= 2 and segments[1] == "output":
            target = _walk_path(node_states[head].get("output"), segments[2:])
        else:
            raise _NodeError(f"json.stringify: unknown ref {ref!r}")
        if target is _MISSING_PATH:
            raise _NodeError(f"json.stringify: ref {ref!r} resolved to nothing")
        try:
            return json.dumps(target, indent=2 if pretty else None, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise _NodeError(f"json.stringify failed: {exc}")

    def _run_hash_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        import hashlib
        params = node.get("params", {}) or {}
        algo = str(params.get("algo", "sha256")).lower().strip()
        value = _hydrate(str(params.get("value", "")), node_states, input_)
        if algo not in {"md5", "sha1", "sha256", "sha512"}:
            raise _NodeError(f"hash algo not supported: {algo!r}")
        digest = hashlib.new(algo, value.encode("utf-8")).hexdigest()
        return {"algo": algo, "hex": digest}

    def _run_base64_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        import base64 as _b64
        params = node.get("params", {}) or {}
        op = str(params.get("op", "encode")).lower().strip()
        value = _hydrate(str(params.get("value", "")), node_states, input_)
        try:
            if op == "encode":
                result = _b64.b64encode(value.encode("utf-8")).decode("ascii")
            elif op == "decode":
                result = _b64.b64decode(value.encode("ascii")).decode("utf-8")
            else:
                raise _NodeError(f"base64 op not supported: {op!r}")
        except Exception as exc:
            raise _NodeError(f"base64 {op} failed: {exc}")
        return {"op": op, "value": result}

    def _run_datetime_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        from datetime import timedelta
        params = node.get("params", {}) or {}
        op = str(params.get("op", "now")).strip()
        if op == "now":
            now = _utcnow()
            return {"iso": now.isoformat(), "unix": int(now.timestamp())}
        if op == "format":
            iso = _hydrate(str(params.get("value", "")), node_states, input_).strip()
            fmt = str(params.get("format", "%Y-%m-%d %H:%M:%S"))
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                raise _NodeError(f"datetime.format: not ISO 8601: {iso!r}")
            return {"value": dt.strftime(fmt)}
        if op == "add":
            iso = _hydrate(str(params.get("value", "")), node_states, input_).strip()
            try:
                dt = datetime.fromisoformat(iso) if iso else _utcnow()
                seconds = int(params.get("seconds", 0))
            except ValueError as exc:
                raise _NodeError(f"datetime.add: {exc}")
            shifted = dt + timedelta(seconds=seconds)
            return {"iso": shifted.isoformat(), "unix": int(shifted.timestamp())}
        if op == "diff":
            a_iso = _hydrate(str(params.get("a", "")), node_states, input_).strip()
            b_iso = _hydrate(str(params.get("b", "")), node_states, input_).strip()
            try:
                a = datetime.fromisoformat(a_iso)
                b = datetime.fromisoformat(b_iso)
            except ValueError as exc:
                raise _NodeError(f"datetime.diff: {exc}")
            delta = (a - b).total_seconds()
            return {"seconds": delta, "minutes": delta / 60, "hours": delta / 3600}
        raise _NodeError(f"datetime op not supported: {op!r}")

    def _resolve_workspace_path(self, raw: str, workspace) -> Any:
        """Return an absolute path inside workspace, or raise _NodeError
        if the request escapes the workspace dir."""
        from pathlib import Path as _P
        ws = _P(str(workspace)).resolve()
        # Reject anchors that try to escape; resolve relative to workspace.
        candidate = (ws / raw).resolve() if not _P(raw).is_absolute() else _P(raw).resolve()
        try:
            candidate.relative_to(ws)
        except ValueError:
            raise _NodeError(f"path outside workspace: {raw!r}")
        return candidate

    def _run_file_read_node(
        self, node: dict, node_states: dict[str, dict], input_: dict, workspace,
    ) -> dict:
        params = node.get("params", {}) or {}
        path_tpl = _hydrate(str(params.get("path", "")), node_states, input_).strip()
        if not path_tpl:
            raise _NodeError("file.read: 'path' is required")
        target = self._resolve_workspace_path(path_tpl, workspace)
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise _NodeError(f"file.read: not found: {path_tpl}")
        except OSError as exc:
            raise _NodeError(f"file.read: {exc}")
        # Cap returned content so a huge file can't bloat node_states.
        max_bytes = int(params.get("max_bytes", 256 * 1024))
        truncated = len(content) > max_bytes
        if truncated:
            content = content[:max_bytes]
        return {"path": str(target), "content": content, "truncated": truncated}

    def _run_file_write_node(
        self, node: dict, node_states: dict[str, dict], input_: dict, workspace,
    ) -> dict:
        params = node.get("params", {}) or {}
        path_tpl = _hydrate(str(params.get("path", "")), node_states, input_).strip()
        if not path_tpl:
            raise _NodeError("file.write: 'path' is required")
        content = _hydrate(str(params.get("content", "")), node_states, input_)
        append = bool(params.get("append", False))
        target = self._resolve_workspace_path(path_tpl, workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        try:
            with open(target, mode, encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            raise _NodeError(f"file.write: {exc}")
        return {"path": str(target), "bytes_written": len(content), "append": append}

    def _run_context_node(
        self,
        node: dict,
        node_states: dict[str, dict],
        input_: dict,
        workspace,
    ) -> dict:
        """Materialise a "context pack" on the recipient's workspace.

        Ships three things along with the workflow:
          • files     — list of {path, content} written into the workspace
                        (path is workspace-relative; templated content is
                        hydrated). Useful for a CLAUDE.md, reference data,
                        a transcript of prior conversation, etc.
          • system_prompt — instructions downstream agent nodes can opt
                        into via {{thisId.output.system_prompt}}.
          • notes     — free-text description; not used by code, but kept
                        in the run record so the recipient can see what
                        the sender intended.

        Output dict is referenced by downstream agents so the same context
        bundle can drive multiple agent nodes in one workflow.
        """
        params = node.get("params", {}) or {}
        files = params.get("files") or []
        if not isinstance(files, list):
            raise _NodeError("context 'files' must be a list of {path, content}")
        system_prompt = _hydrate(
            str(params.get("system_prompt", "")), node_states, input_,
        )
        notes = _hydrate(str(params.get("notes", "")), node_states, input_)

        written: list[dict] = []
        for entry in files:
            if not isinstance(entry, dict):
                raise _NodeError("each context file must be {path, content}")
            path_raw = _hydrate(str(entry.get("path", "")), node_states, input_).strip()
            if not path_raw:
                raise _NodeError("context file is missing 'path'")
            content = _hydrate(str(entry.get("content", "")), node_states, input_)
            target = self._resolve_workspace_path(path_raw, workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(content, encoding="utf-8")
            except OSError as exc:
                raise _NodeError(f"context: write {path_raw}: {exc}")
            written.append({"path": str(target), "bytes": len(content)})

        return {
            "system_prompt": system_prompt,
            "files_written": written,
            "notes": notes,
        }

    async def _run_sound_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Play a macOS system sound via afplay. Sound name must match a
        file in /System/Library/Sounds (without .aiff)."""
        params = node.get("params", {}) or {}
        sound = str(params.get("sound", "Ping")).strip() or "Ping"
        # Allow only alphanumerics — afplay path is built from this.
        if not sound.replace("_", "").isalnum():
            raise _NodeError(f"notify.sound: invalid sound name {sound!r}")
        path = f"/System/Library/Sounds/{sound}.aiff"
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/afplay", path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except FileNotFoundError:
            raise _NodeError("notify.sound: afplay not available (not macOS?)")
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise _NodeError("notify.sound: afplay timed out")
        return {"sound": sound, "exit_code": proc.returncode}

    # ── AI convenience nodes (all delegate to _invoke_llm) ─────────────

    async def _run_ai_classify_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
        *, workspace, allowed_tools, can_use_tool, sender_id, send_event,
    ) -> dict:
        params = node.get("params", {}) or {}
        text = _hydrate(str(params.get("input", "")), node_states, input_).strip()
        categories = params.get("categories") or []
        if not isinstance(categories, list) or not categories:
            raise _NodeError("ai.classify: 'categories' must be a non-empty list")
        if not text:
            raise _NodeError("ai.classify: 'input' is empty after hydration")
        cat_list = ", ".join(str(c) for c in categories)
        prompt = (
            f"Classify the text below into exactly ONE of these categories: {cat_list}.\n"
            f"Respond with ONLY the category name. No explanation, no quotes, no punctuation.\n\n"
            f"Text:\n{text}"
        )
        answer = await self._invoke_llm(
            prompt, workspace=workspace, allowed_tools=[],
            can_use_tool=can_use_tool, sender_id=sender_id, send_event=send_event,
        )
        chosen = answer.strip().splitlines()[0].strip() if answer.strip() else ""
        # Snap to the closest declared category by case-insensitive equality.
        normalized = {str(c).strip().lower(): str(c) for c in categories}
        match = normalized.get(chosen.lower(), chosen)
        return {"category": match, "raw": answer, "categories": list(categories)}

    async def _run_ai_extract_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
        *, workspace, allowed_tools, can_use_tool, sender_id, send_event,
    ) -> Any:
        params = node.get("params", {}) or {}
        text = _hydrate(str(params.get("input", "")), node_states, input_).strip()
        schema = params.get("schema")
        if not text:
            raise _NodeError("ai.extract: 'input' is empty after hydration")
        if schema in (None, ""):
            raise _NodeError("ai.extract: 'schema' is required")
        schema_str = (
            schema if isinstance(schema, str)
            else json.dumps(schema, indent=2, ensure_ascii=False)
        )
        prompt = (
            "Extract structured data from the text below into a JSON object matching this schema:\n"
            f"{schema_str}\n\n"
            "Respond with ONLY a valid JSON object — no markdown fences, no explanation.\n\n"
            f"Text:\n{text}"
        )
        answer = await self._invoke_llm(
            prompt, workspace=workspace, allowed_tools=[],
            can_use_tool=can_use_tool, sender_id=sender_id, send_event=send_event,
        )
        # Strip common markdown JSON fences just in case the model
        # ignored the instruction; better lenient parsing than failing.
        cleaned = answer.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise _NodeError(f"ai.extract: model did not return JSON: {exc}")

    async def _run_ai_summarize_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
        *, workspace, allowed_tools, can_use_tool, sender_id, send_event,
    ) -> dict:
        params = node.get("params", {}) or {}
        text = _hydrate(str(params.get("input", "")), node_states, input_).strip()
        words = int(params.get("max_words", 50) or 50)
        if not text:
            raise _NodeError("ai.summarize: 'input' is empty")
        prompt = (
            f"Summarize the text below in {words} words or fewer. "
            f"Respond with ONLY the summary — no preamble, no quotes.\n\n"
            f"Text:\n{text}"
        )
        summary = await self._invoke_llm(
            prompt, workspace=workspace, allowed_tools=[],
            can_use_tool=can_use_tool, sender_id=sender_id, send_event=send_event,
        )
        return {"summary": summary.strip(), "max_words": words}

    async def _run_ai_judge_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
        *, workspace, allowed_tools, can_use_tool, sender_id, send_event,
    ) -> dict:
        params = node.get("params", {}) or {}
        text = _hydrate(str(params.get("input", "")), node_states, input_).strip()
        criterion = _hydrate(str(params.get("criterion", "")), node_states, input_).strip()
        if not text or not criterion:
            raise _NodeError("ai.judge: 'input' and 'criterion' are required")
        prompt = (
            "Score the text below from 1 (terrible) to 10 (excellent) against this criterion:\n"
            f"{criterion}\n\n"
            "Respond with EXACTLY one line: the integer score, a space, then a one-sentence reason. "
            "Example: \"7 Clear thesis but examples are weak.\"\n\n"
            f"Text:\n{text}"
        )
        answer = await self._invoke_llm(
            prompt, workspace=workspace, allowed_tools=[],
            can_use_tool=can_use_tool, sender_id=sender_id, send_event=send_event,
        )
        line = answer.strip().splitlines()[0] if answer.strip() else ""
        parts = line.split(" ", 1)
        try:
            score = int(parts[0])
        except (ValueError, IndexError):
            score = 0
        reason = parts[1].strip() if len(parts) > 1 else ""
        return {"score": max(1, min(10, score)), "reason": reason, "raw": answer}

    def _run_log_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Like notify but quiet — just writes to the daemon log instead
        of popping a macOS notification. Useful for debugging."""
        params = node.get("params", {}) or {}
        level = str(params.get("level", "info")).lower().strip()
        message = _hydrate(str(params.get("message", "")), node_states, input_)
        if level == "error":
            logger.error("[workflow] %s", message)
        elif level in ("warn", "warning"):
            logger.warning("[workflow] %s", message)
        else:
            logger.info("[workflow] %s", message)
        return {"level": level, "message": message}

    def _run_random_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Generate a random value. Params:
            kind: "uuid" | "int" | "float"
            min, max: bounds for int/float (inclusive int, [min, max) float)
        """
        import random as _random
        import uuid as _uuid
        params = node.get("params", {}) or {}
        kind = str(params.get("kind", "uuid")).lower().strip()
        if kind == "uuid":
            return {"kind": "uuid", "value": str(_uuid.uuid4())}
        if kind == "int":
            try:
                lo = int(params.get("min", 0))
                hi = int(params.get("max", 100))
            except (TypeError, ValueError):
                raise _NodeError("random int requires integer min/max")
            if hi < lo:
                lo, hi = hi, lo
            return {"kind": "int", "value": _random.randint(lo, hi)}
        if kind == "float":
            try:
                lo = float(params.get("min", 0.0))
                hi = float(params.get("max", 1.0))
            except (TypeError, ValueError):
                raise _NodeError("random float requires numeric min/max")
            if hi < lo:
                lo, hi = hi, lo
            return {"kind": "float", "value": _random.uniform(lo, hi)}
        raise _NodeError(f"random kind must be uuid|int|float (got {kind!r})")

    async def _run_wait_until_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Sleep until a specific ISO 8601 timestamp. If the time has
        already passed, returns immediately. Capped at max_wait_s so a
        bad template can't pin the engine forever."""
        params = node.get("params", {}) or {}
        until_tpl = _hydrate(str(params.get("until", "")), node_states, input_).strip()
        if not until_tpl:
            raise _NodeError("wait_until missing 'until'")
        try:
            target = datetime.fromisoformat(until_tpl)
        except ValueError:
            raise _NodeError(f"wait_until 'until' is not ISO 8601: {until_tpl!r}")
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)

        try:
            max_wait = int(params.get("max_wait_s", 86400))
        except (TypeError, ValueError):
            max_wait = 86400
        max_wait = min(max(max_wait, 1), 7 * 24 * 3600)  # cap at one week

        now = _utcnow()
        delta = (target - now).total_seconds()
        wait = max(0.0, min(delta, float(max_wait)))
        await asyncio.sleep(wait)
        return {
            "waited_seconds": wait,
            "fired_at": _utcnow().isoformat(),
            "target": target.isoformat(),
        }

    def _run_notify_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        title = _hydrate(str(params.get("title", "Workflow")), node_states, input_)
        body = _hydrate(str(params.get("body", "")), node_states, input_)
        self.local_state._push_notification(title, "", body)
        return {"title": title, "body": body}

    def _run_code_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> Any:
        # Trusted-author expression sandbox. Anyone who can author a
        # workflow can already trigger agents on the recipient — this
        # eval is no broader a foothold than that.
        params = node.get("params", {}) or {}
        code = str(params.get("code", "")).strip()
        if not code:
            raise _NodeError("transform.code missing 'code'")

        namespace: dict[str, Any] = {
            "ctx": input_,
            "json": json,
            "math": math,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "dict": dict,
            "list": list,
            "sum": sum,
            "min": min,
            "max": max,
            "sorted": sorted,
            "any": any,
            "all": all,
        }
        for nid, state in node_states.items():
            if state.get("status") == NodeStatus.completed.value:
                namespace[nid] = state.get("output")
        try:
            return eval(code, {"__builtins__": {}}, namespace)
        except Exception as exc:
            raise _NodeError(f"code eval failed: {exc}")

    async def _run_http_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        method = str(params.get("method", "GET")).upper().strip() or "GET"
        url_tpl = str(params.get("url", "")).strip()
        if not url_tpl:
            raise _NodeError("http.request missing 'url'")
        url = _hydrate(url_tpl, node_states, input_)

        headers_in = params.get("headers") or {}
        if not isinstance(headers_in, dict):
            raise _NodeError("http.request 'headers' must be an object")
        headers = {
            str(k): _hydrate(str(v), node_states, input_)
            for k, v in headers_in.items()
        }

        body_tpl = params.get("body")
        body_text: Optional[str] = None
        if body_tpl is not None and str(body_tpl) != "":
            body_text = _hydrate(str(body_tpl), node_states, input_)

        try:
            timeout_s = float(params.get("timeout_s", 30) or 30)
        except (TypeError, ValueError):
            timeout_s = 30.0

        try:
            async with httpx.AsyncClient(
                timeout=timeout_s, follow_redirects=False,
            ) as client:
                resp = await client.request(
                    method, url, headers=headers, content=body_text,
                )
        except httpx.HTTPError as exc:
            raise _NodeError(f"http.request failed: {exc}")

        parsed_json: Any = None
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype.lower():
            try:
                parsed_json = resp.json()
            except ValueError:
                parsed_json = None

        return {
            "status": resp.status_code,
            "body": resp.text,
            "json": parsed_json,
            "headers": dict(resp.headers),
        }

    async def _run_delay_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        try:
            seconds = int(params.get("seconds", 1))
        except (TypeError, ValueError):
            raise _NodeError("delay 'seconds' must be an integer")
        seconds = min(max(seconds, 1), 3600)
        await asyncio.sleep(seconds)
        return {"delayed_for": seconds}

    def _run_end_success_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> Any:
        params = node.get("params", {}) or {}
        msg_tpl = params.get("message")
        msg = (
            _hydrate(str(msg_tpl), node_states, input_)
            if msg_tpl else "ended"
        )
        raise _EndRun(status=WorkflowRunStatus.completed, message=msg)

    def _run_end_error_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> Any:
        params = node.get("params", {}) or {}
        msg_tpl = params.get("message")
        msg = (
            _hydrate(str(msg_tpl), node_states, input_)
            if msg_tpl else "workflow ended with error"
        )
        raise _EndRun(status=WorkflowRunStatus.failed, message=msg)

    # ────────────────────────────────────────────────────────────────────
    # State writeback
    # ────────────────────────────────────────────────────────────────────

    async def _set_node_state(
        self,
        run_id: UUID,
        node_states: dict[str, dict],
        node_id: str,
        *,
        status: Optional[NodeStatus] = None,
        output: Any = _MISSING,
        started_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        ns = node_states[node_id]
        if status is not None:
            ns["status"] = status.value
        if output is not _MISSING:
            ns["output"] = output
        if started_at is not None:
            ns["started_at"] = started_at.isoformat()
        if ended_at is not None:
            ns["ended_at"] = ended_at.isoformat()
        if error is not None:
            ns["error"] = error
        await self._patch_run(run_id, node_states=node_states)

    async def _patch_run(
        self,
        run_id: UUID,
        *,
        status: Optional[WorkflowRunStatus] = None,
        node_states: Optional[dict] = None,
        error: Optional[str] = None,
        ended: bool = False,
    ) -> None:
        body: dict[str, Any] = {}
        if status is not None:
            body["status"] = status.value
        if node_states is not None:
            body["node_states"] = node_states
        if error is not None:
            body["error"] = error
        if ended:
            body["ended"] = True
        if not body:
            return
        try:
            async with self._client_factory() as client:
                await client.patch(
                    f"{self.broker_url}/runs/{run_id}",
                    json=body,
                    headers={"Authorization": f"Bearer {self.broker_token}"},
                )
        except httpx.HTTPError:
            logger.exception("PATCH /runs/%s failed", run_id)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


class _NodeError(Exception):
    """Per-node failure that should halt the run and surface a message."""


class _EndRun(Exception):
    """Raised by end.success / end.error nodes to halt the engine cleanly."""

    def __init__(self, status: WorkflowRunStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _WorkflowGraphError(Exception):
    """Definition is structurally invalid (cycle, dangling edge, etc.)."""


def _topo_sort(nodes: list[dict], edges: list[dict]) -> list[str]:
    """Kahn's algorithm. Ignores edges whose endpoints aren't in `nodes`
    so a stale edge from a deleted node doesn't break the run."""
    ids = {n["id"] for n in nodes}
    indegree: dict[str, int] = {nid: 0 for nid in ids}
    adj: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in edges:
        src = e.get("from") or e.get("from_node")
        dst = e.get("to") or e.get("to_node")
        if src not in ids or dst not in ids:
            continue
        adj[src].append(dst)
        indegree[dst] += 1

    frontier = sorted(nid for nid, d in indegree.items() if d == 0)
    order: list[str] = []
    while frontier:
        nid = frontier.pop(0)
        order.append(nid)
        for nxt in adj[nid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                frontier.append(nxt)
        frontier.sort()

    if len(order) != len(ids):
        raise _WorkflowGraphError("workflow graph contains a cycle")
    return order


_MISSING_PATH = object()


def _walk_path(value: Any, segments: list[str]) -> Any:
    """Walk a dotted path into a dict/list value. List indices are
    accepted as numeric strings. Returns _MISSING_PATH on any miss so
    the caller can leave the unresolved placeholder in place."""
    for seg in segments:
        if isinstance(value, dict) and seg in value:
            value = value[seg]
        elif isinstance(value, list):
            try:
                value = value[int(seg)]
            except (ValueError, IndexError):
                return _MISSING_PATH
        else:
            return _MISSING_PATH
    return value


def _hydrate(template: str, node_states: dict[str, dict], ctx: dict) -> str:
    """Replace {{nN.output}}, {{nN.output.key.sub}}, {{ctx.key}}, and
    {{ctx.key.sub}} placeholders.

    Dotted paths descend into dicts (by key) and lists (by integer index).
    Unresolved references are left in the output as-is so the user can
    see exactly which placeholder was wrong inside a hydrated string.
    """

    def repl(match: re.Match[str]) -> str:
        expr = match.group(1)
        parts = expr.split(".")
        if not parts:
            return match.group(0)

        head = parts[0]
        if head == "ctx":
            value = _walk_path(ctx, parts[1:])
        elif head in node_states and len(parts) >= 2 and parts[1] == "output":
            output = node_states[head].get("output")
            value = _walk_path(output, parts[2:])
        else:
            return match.group(0)

        if value is _MISSING_PATH:
            return match.group(0)
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(repl, template)
