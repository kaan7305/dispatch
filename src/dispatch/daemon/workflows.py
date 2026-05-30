"""Daemon-side workflow executor.

The broker is the storage / orchestration system of record for workflow
definitions and run records — but ALL execution happens in the daemon.
The broker only holds the dispatcher's JWT; the daemon has the device
keys, the trust scopes, and the live LocalState that the SPA already
watches for dispatch progress. By running the engine here we reuse the
same trust + signing + delivery path that powers manual /api/compose.

Flow:
  1. Broker accepts POST /workflows/{id}/run, creates a run row, and
     pushes {type:"workflow_run_start", run_id, workflow_id, definition,
     input, user_id} down THIS daemon's broker WS.
  2. handle_broker calls engine.start_run(...), which spawns _execute.
  3. _execute walks nodes in topo order, dispatching/notifying/waiting.
     Every state transition PATCHes the broker so GET /runs/{id} returns
     a live view. The SPA polls /api/runs/{id} once per second.
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

from dispatch.shared.schema import (
    DispatchCreateRequest,
    DispatchStatus,
    NodeState,
    NodeStatus,
    WorkflowRunStatus,
)

logger = logging.getLogger("dispatch.daemon.workflows")

# Polling cadence for both dispatch terminal-status detection and the
# wait_reply node. Tight enough that the SPA's 1Hz GET shows live state
# transitions, loose enough not to pin a CPU.
POLL_INTERVAL_S = 1.0

# Statuses we treat as "the dispatch will produce no more output."
_TERMINAL_STATUSES = {
    DispatchStatus.completed,
    DispatchStatus.denied,
    DispatchStatus.failed,
    DispatchStatus.expired,
    DispatchStatus.cancelled,
}

# {{nN.output}} and {{ctx.key}} — single curly braces are NOT recognised
# (avoids accidental substitution inside JSON example bodies).
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
        self.runs: dict[UUID, asyncio.Task] = {}
        # Indirection so tests can substitute a transport-mocked client
        # without having to monkey-patch httpx globally.
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=10.0)
        )

    # ────────────────────────────────────────────────────────────────────
    # Entry points
    # ────────────────────────────────────────────────────────────────────

    async def start_run(
        self,
        run_id: UUID,
        workflow_id: UUID,
        definition: dict,
        input_: dict,
        user_id: str,
    ) -> None:
        # Already running? The broker won't send a duplicate start, but
        # be defensive — re-starting would create two writers racing on
        # the same run row.
        if run_id in self.runs and not self.runs[run_id].done():
            return
        task = asyncio.create_task(
            self._execute(run_id, workflow_id, definition, input_, user_id)
        )
        self.runs[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self.runs.pop(rid, None))

    def cancel(self, run_id: UUID) -> bool:
        """Cancel a running workflow. In-flight dispatches keep going on
        the recipient side — they're already signed and the recipient owns
        approval. Returns False if the run isn't active."""
        task = self.runs.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def _execute(
        self,
        run_id: UUID,
        workflow_id: UUID,
        definition: dict,
        input_: dict,
        user_id: str,
    ) -> None:
        nodes = definition.get("nodes", []) or []
        edges = definition.get("edges", []) or []
        nodes_by_id: dict[str, dict] = {n["id"]: n for n in nodes}

        # Per-node execution state. node_states maps node id → NodeState
        # serialised to dict for the broker PATCH payload.
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
            return

        # Exactly one trigger.* node is required (trigger.manual or
        # trigger.cron). The broker would ideally reject malformed
        # workflows at save-time, but the daemon double-checks so a
        # corrupt definition can't crash the engine.
        triggers = [n for n in nodes if str(n.get("type", "")).startswith("trigger.")]
        if len(triggers) != 1:
            await self._patch_run(
                run_id,
                status=WorkflowRunStatus.failed,
                node_states=node_states,
                error=f"expected exactly one trigger.* node, found {len(triggers)}",
                ended=True,
            )
            return

        # Track which (source, source_port) pairs were "taken" by branch
        # nodes. A node is skipped if every incoming edge originates from
        # either a skipped predecessor or a not-taken branch port.
        taken_ports: set[tuple[str, str]] = set()  # {(source_id, port)}
        skipped_nodes: set[str] = set()

        def is_edge_alive(edge: dict) -> bool:
            src = edge.get("from")
            sport = edge.get("from_port", "out")
            if src in skipped_nodes:
                return False
            src_node = nodes_by_id.get(src)
            # Branch nodes gate their outgoing edges; non-branch sources
            # always pass their single 'out' through when reached.
            if src_node and src_node.get("type") == "branch":
                return (src, sport) in taken_ports
            return True

        try:
            for node_id in order:
                node = nodes_by_id[node_id]
                ntype = node.get("type", "")
                incoming = [e for e in edges if e.get("to") == node_id]

                # Trigger always runs; other nodes need at least one live
                # incoming edge. Orphans (no incoming, not trigger) are
                # treated as skipped — they can't have been reached.
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
                    if ntype == "trigger.manual":
                        output: Any = input_
                    elif ntype == "trigger.cron":
                        # Scheduler populates input_ from the node's
                        # static params.input; here we just pass it through
                        # so downstream nodes see ctx.* like trigger.manual.
                        output = input_
                    elif ntype == "dispatch":
                        output = await self._run_dispatch_node(
                            run_id, node, node_states, input_,
                        )
                    elif ntype == "dispatch.multi":
                        output = await self._run_multi_dispatch_node(
                            run_id, node, node_states, input_,
                        )
                    elif ntype == "branch":
                        # Branch returns the port name to pass through;
                        # we DON'T store the port in 'output' (kept for
                        # downstream templates), only in taken_ports.
                        branch_result = self._run_branch_node(node, node_states, input_)
                        taken_ports.add((node_id, branch_result["port"]))
                        output = branch_result["value"]
                    elif ntype == "notify":
                        output = self._run_notify_node(node, node_states, input_)
                    elif ntype == "wait_reply":
                        output = await self._run_wait_reply_node(node, node_states)
                    elif ntype == "transform.code":
                        output = self._run_code_node(node, node_states, input_)
                    elif ntype == "http.request":
                        output = await self._run_http_node(node, node_states, input_)
                    elif ntype == "delay":
                        output = await self._run_delay_node(node, node_states, input_)
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
                    return
                except _EndRun as exc:
                    # end.success / end.error halt the run. The originating
                    # node itself ran successfully — record the message as
                    # its output before we close out the run row.
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
                    return

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
            return

        await self._patch_run(
            run_id,
            status=WorkflowRunStatus.completed,
            node_states=node_states,
            ended=True,
        )

    # ────────────────────────────────────────────────────────────────────
    # Per-node executors
    # ────────────────────────────────────────────────────────────────────

    async def _run_dispatch_node(
        self,
        run_id: UUID,
        node: dict,
        node_states: dict[str, dict],
        input_: dict,
    ) -> str:
        params = node.get("params", {}) or {}
        recipient_id = params.get("recipient_id")
        if not recipient_id:
            raise _NodeError("dispatch node missing recipient_id")
        task_template = params.get("task")
        if not task_template:
            raise _NodeError("dispatch node missing task")
        task_text = _hydrate(task_template, node_states, input_)

        body = DispatchCreateRequest(
            recipient_id=str(recipient_id),
            task=task_text,
            expires_in_seconds=int(params.get("expires_in_seconds", 3600)),
            metadata={
                "workflow_run_id": str(run_id),
                "node_id": node["id"],
            },
        ).model_dump(exclude_none=True)

        async with self._client_factory() as client:
            try:
                resp = await client.post(
                    f"{self.broker_url}/dispatch",
                    json=body,
                    headers={"Authorization": f"Bearer {self.broker_token}"},
                )
            except httpx.HTTPError as exc:
                raise _NodeError(f"broker unreachable: {exc}")

        if resp.status_code >= 400:
            # Surface broker's structured error message verbatim so the
            # SPA can show why the trust check / signing failed.
            try:
                detail = resp.json().get("detail") or resp.text
            except ValueError:
                detail = resp.text
            raise _NodeError(f"broker rejected dispatch ({resp.status_code}): {detail}")

        data = resp.json()
        dispatch_id_str = data.get("dispatch_id")
        if not dispatch_id_str:
            # Fan-out isn't valid here (MVP: one node = one recipient),
            # but if a future change adds it, fail loud.
            raise _NodeError(f"unexpected /dispatch response: {data}")

        try:
            dispatch_id = UUID(dispatch_id_str)
        except ValueError:
            raise _NodeError(f"broker returned non-UUID dispatch_id: {dispatch_id_str}")

        # Record dispatch_id immediately so the SPA can drill into the
        # live dispatch view via the existing /api/dispatch/{id} route.
        ns = node_states[node["id"]]
        ns["dispatch_id"] = str(dispatch_id)

        terminal = await self._await_dispatch_terminal(dispatch_id)

        if terminal is DispatchStatus.completed:
            entry = self.local_state.entries.get(dispatch_id)
            agent_text = ""
            if entry is not None:
                for ev in reversed(entry.events):
                    if isinstance(ev, dict) and ev.get("type") == "agent_text":
                        agent_text = str(ev.get("data", {}).get("text", ""))
                        break
            return agent_text

        # denied / failed / expired / cancelled — fold into the node error.
        raise _NodeError(f"dispatch terminated as {terminal.value}")

    def _run_branch_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        """Evaluate the condition + return which output port to take.

        Params: { left: str, op: str ("==", "!=", "contains"), right: str }
        Both sides go through template hydration. Returns:
          { port: "out_true"|"out_false", value: dict for downstream nodes }
        """
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

    async def _run_multi_dispatch_node(
        self,
        run_id: UUID,
        node: dict,
        node_states: dict[str, dict],
        input_: dict,
    ) -> dict:
        """Fan-out: send the same task to N recipients in one call, wait
        until every dispatch reaches a terminal status, return per-recipient
        results. Half-failure is tolerated — only fails the node if EVERY
        recipient failed."""
        params = node.get("params", {}) or {}
        recipient_ids = params.get("recipient_ids") or []
        if not isinstance(recipient_ids, list) or not recipient_ids:
            raise _NodeError("multi-dispatch needs at least one recipient_id")
        task = _hydrate(str(params.get("task", "")), node_states, input_)
        if not task:
            raise _NodeError("multi-dispatch task is empty")

        scopes = params.get("scopes") or {}
        timeout_s = int(params.get("timeout_s") or 3600)
        async with self._client_factory() as client:
            resp = await client.post(
                f"{self.broker_url}/dispatch",
                json={
                    "recipient_ids": list(recipient_ids),
                    "task": task,
                    "expires_in_seconds": timeout_s,
                    "metadata": {
                        "workflow_run_id": str(run_id),
                        "node_id": node.get("id", ""),
                    },
                },
                headers={"Authorization": f"Bearer {self.broker_token}"},
            )
        if resp.status_code != 200:
            raise _NodeError(f"broker rejected fan-out: {resp.text[:200]}")
        body = resp.json()
        # Single-recipient back-compat: the broker returns the legacy shape
        # if there's only one recipient. Normalize to the fan-out shape.
        if "dispatches" not in body:
            body = {"dispatches": [body], "failures": []}

        # Poll every dispatch in parallel until each is terminal.
        async def wait_one(dispatch_entry: dict) -> dict:
            from uuid import UUID as _UUID
            did = _UUID(dispatch_entry["dispatch_id"])
            terminal = await self._await_dispatch_terminal(did)
            output = ""
            entry = self.local_state.entries.get(did)
            if entry is not None:
                for ev in reversed(entry.events):
                    if ev.get("type") == "agent_text":
                        output = str(ev.get("data", {}).get("text", "") or "")
                        break
            return {
                "recipient_id": dispatch_entry["recipient_id"],
                "dispatch_id": str(did),
                "status": terminal.value,
                "output": output,
            }

        results = await asyncio.gather(
            *(wait_one(d) for d in body["dispatches"]),
            return_exceptions=True,
        )
        completed = [r for r in results if isinstance(r, dict) and r.get("status") == "completed"]
        all_failed = (
            len(completed) == 0
            and len(body.get("failures", [])) + len(results) > 0
        )
        if all_failed:
            raise _NodeError("every recipient failed or denied the dispatch")
        return {
            "dispatches": [r for r in results if isinstance(r, dict)],
            "failures": body.get("failures", []),
            "completed_count": len(completed),
        }

    def _run_notify_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> dict:
        params = node.get("params", {}) or {}
        title = _hydrate(str(params.get("title", "Workflow")), node_states, input_)
        body = _hydrate(str(params.get("body", "")), node_states, input_)
        self.local_state._push_notification(title, "", body)
        return {"title": title, "body": body}

    async def _run_wait_reply_node(
        self, node: dict, node_states: dict[str, dict],
    ) -> str:
        params = node.get("params", {}) or {}
        from_recipient_id = params.get("from_recipient_id")
        if not from_recipient_id:
            raise _NodeError("wait_reply node missing from_recipient_id")

        # Snapshot the inbox at wait-start so we only count dispatches that
        # arrive AFTER this point. Otherwise an old dispatch from the same
        # sender would resolve the wait immediately.
        started = _utcnow()
        baseline = set(self.local_state.entries.keys())

        timeout_s = float(params.get("timeout_seconds", 3600))
        deadline = asyncio.get_event_loop().time() + timeout_s

        while True:
            for did, entry in list(self.local_state.entries.items()):
                if did in baseline:
                    continue
                payload = entry.payload
                if payload.sender_id != from_recipient_id:
                    continue
                # Belt-and-braces — created_at should always be ≥ started
                # since baseline excludes pre-existing entries, but check.
                if payload.created_at >= started:
                    return payload.task

            if asyncio.get_event_loop().time() >= deadline:
                raise _NodeError(
                    f"no reply from {from_recipient_id} within {int(timeout_s)}s"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    def _run_code_node(
        self, node: dict, node_states: dict[str, dict], input_: dict,
    ) -> Any:
        # User-written Python expression. Sandboxed by stripping __builtins__
        # and only injecting a curated namespace. This is "trusted-author"
        # safe, not "untrusted-author" safe — anyone who can edit the
        # workflow definition can already trigger dispatches.
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
        # asyncio.sleep is cancellable — task.cancel() will propagate.
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
            # Best-effort — the broker may be flaky mid-run, but we don't
            # want a transient PATCH failure to abort an in-flight workflow.
            logger.exception("PATCH /runs/%s failed", run_id)

    # ────────────────────────────────────────────────────────────────────
    # Dispatch terminal-status detection
    # ────────────────────────────────────────────────────────────────────

    async def _await_dispatch_terminal(self, dispatch_id: UUID) -> DispatchStatus:
        # local_state.entries is updated synchronously by on_status from
        # the daemon's broker WS reader. We poll it instead of subscribing
        # so we don't have to thread engine-specific callbacks through the
        # LocalState wiring just for MVP.
        while True:
            entry = self.local_state.entries.get(dispatch_id)
            if entry is not None and entry.status in _TERMINAL_STATUSES:
                return entry.status
            await asyncio.sleep(POLL_INTERVAL_S)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


class _NodeError(Exception):
    """Per-node failure that should halt the run and surface a message."""


class _EndRun(Exception):
    """Raised by end.success / end.error nodes to halt the engine cleanly.

    Carries the terminal run status + a (templatable) message that gets
    written to the originating node's output and — for failures — into
    the run row's `error` column.
    """

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

    # Stable ordering — sort the initial frontier so a workflow's run
    # order is deterministic across daemon restarts.
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


def _hydrate(template: str, node_states: dict[str, dict], ctx: dict) -> str:
    """Replace {{nN.output}} and {{ctx.key}} placeholders.

    Only `.output` for node refs and shallow `ctx.<key>` for the input
    bag — deep paths aren't supported yet. Unknown refs are left as-is
    so the user can see exactly which placeholder was wrong in the
    dispatched task body.
    """

    def repl(match: re.Match[str]) -> str:
        expr = match.group(1)
        if expr.startswith("ctx."):
            key = expr[4:]
            if key in ctx:
                return str(ctx[key])
            return match.group(0)
        if "." in expr:
            node_id, attr = expr.split(".", 1)
            if attr == "output" and node_id in node_states:
                output = node_states[node_id].get("output")
                return "" if output is None else str(output)
        return match.group(0)

    return _TEMPLATE_RE.sub(repl, template)
