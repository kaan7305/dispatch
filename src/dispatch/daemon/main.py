"""Recipient daemon — pure WebSocket client.

Usage:
  dispatch-daemon                       # uses saved config from ~/.dispatch/config.json
  dispatch-daemon --broker URL --token JWT   # explicit; also saves config for next time
  python -m dispatch.daemon ...         # equivalent if installed in a venv

What it does:
  - Connects to the broker via WebSocket as the authenticated user.
  - For each `new_dispatch` from the broker:
      1. Sends `dispatch_status: delivered`.
      2. Waits for `dispatch_decision: accept` or `reject` from the broker
         (which the recipient triggered in the unified web UI).
      3. If accepted, runs run_dispatch() — destructive tool calls cause a
         `permission_request` event to flow back through the broker to the
         UI, where the user clicks Allow / Deny. The decision returns via
         a `tool_approval` message from the broker.
      4. Streams every executor event back to the broker.

The Claude Agent SDK runs in this process using THIS user's
ANTHROPIC_API_KEY. The broker never touches the API key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import certifi
import websockets
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from dispatch.daemon.identity import (
    dispatch_home,
    ensure_enrolled,
    get_private_key,
    load_pins,
    save_pins,
)
from dispatch.executor import run_dispatch
from dispatch.shared import crypto
from dispatch.shared.schema import (
    DispatchEvent,
    DispatchPayload,
    DispatchStatus,
    Scopes,
)
from dispatch.shared.signing import canonical_dispatch_bytes

# Tool arguments that name a filesystem path, by tool. The daemon checks
# these against the trust edge's path allowlist. (Bash commands can embed
# paths arbitrarily and aren't path-checkable — Bash is gated wholesale by
# whether it's in the edge's tool list.)
_PATH_ARGS = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}
DEFAULT_WORKSPACE = Path.cwd() / "workspace"
DISPATCH_DECISION_TIMEOUT_S = 300.0     # how long the recipient has to accept/reject
TOOL_APPROVAL_TIMEOUT_S = 120.0          # how long they have to allow/deny one tool call
FRESHNESS_WINDOW_S = 300.0               # reject dispatches signed > 5 min ago

logger = logging.getLogger("dispatch.daemon")


def _config_path() -> Path:
    return dispatch_home() / "config.json"


def _load_config() -> dict:
    """Reads the daemon config JSON. Returns {} if absent or malformed."""
    try:
        return json.loads(_config_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(**fields: object) -> None:
    """Merge `fields` into the saved config so a bare `dispatch-daemon`
    can resolve broker, token, and device_id on the next run."""
    config = _load_config()
    config.update({k: v for k, v in fields.items() if v is not None})
    path = _config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2))
        path.chmod(0o600)  # bearer token lives here — keep it private
    except OSError:
        logger.warning("could not save config to %s", path)


@dataclass
class DaemonState:
    # dispatch_id (str) → Future resolved with "accept" | "reject"
    pending_decisions: dict[str, asyncio.Future] = field(default_factory=dict)
    # (dispatch_id, request_id) → Future resolved with "allow" | "deny"
    pending_approvals: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)
    # (sender_device, nonce) pairs already accepted this session — replay guard
    seen_nonces: set[tuple[str, str]] = field(default_factory=set)
    # dispatch_id (str) → the running process_dispatch task, so a
    # cancel_dispatch from the broker can stop it.
    running: dict[str, asyncio.Task] = field(default_factory=dict)


def _broker_ws_url(broker: str, token: str) -> str:
    p = urlparse(broker)
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, "/agent/connect", "", f"token={token}", ""))


def _ssl_context_for(url: str) -> ssl.SSLContext | None:
    """Use certifi's CA bundle for TLS so macOS Pythons (which often
    ship without a populated system CA store) can verify the broker's
    certificate. Returns None for non-TLS URLs so plain ws:// still
    works for local dev."""
    if url.startswith("wss://"):
        return ssl.create_default_context(cafile=certifi.where())
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Resolution order for broker/token: CLI flag > env var > saved config file.
    config = _load_config()
    parser = argparse.ArgumentParser(prog="dispatch-daemon")
    parser.add_argument(
        "--broker",
        default=os.environ.get("DISPATCH_BROKER") or config.get("broker") or "http://localhost:8000",
        help="Broker base URL. Default: $DISPATCH_BROKER, then ~/.dispatch/config.json, then localhost.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DISPATCH_TOKEN") or config.get("token"),
        help="JWT bearer token. Default: $DISPATCH_TOKEN, then ~/.dispatch/config.json.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("DISPATCH_WORKSPACE", str(DEFAULT_WORKSPACE)),
        help="Working directory the agent operates in. Default: ./workspace",
    )
    return parser.parse_args(argv)


async def run_session(args: argparse.Namespace) -> int:
    if not args.token:
        print(
            "error: no token. Sign in on the broker's web page, then either:\n"
            "  - run the one-line installer it shows you, or\n"
            "  - run: dispatch-daemon --broker <url> --token <jwt>",
            file=sys.stderr,
        )
        return 2

    # Device enrollment: ensure this machine has an Ed25519 keypair and a
    # broker-issued device_id. Generates the keypair on first run.
    try:
        device_id = await ensure_enrolled(
            args.broker, args.token, _load_config().get("device_id")
        )
    except Exception as exc:
        print(f"[daemon] device enrollment failed: {exc}", file=sys.stderr)
        return 5

    # Remember broker + token + device so future runs can be a bare
    # `dispatch-daemon`.
    _save_config(broker=args.broker, token=args.token, device_id=device_id)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY is not set; the agent will fail to run unless "
            "the Claude Code CLI has its own session.",
            file=sys.stderr,
        )

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    print(f"[daemon] workspace: {workspace}")
    print(f"[daemon] device: {device_id}")

    private_key = get_private_key()
    if private_key is None:
        print("[daemon] no device private key found after enrollment", file=sys.stderr)
        return 5

    state = DaemonState()
    ws_url = _broker_ws_url(args.broker, args.token)
    ssl_ctx = _ssl_context_for(ws_url)
    print(f"[daemon] connecting to broker: {args.broker}")
    try:
        async with websockets.connect(ws_url, max_size=None, ssl=ssl_ctx) as ws:
            # First frame identifies this device to the broker.
            await ws.send(json.dumps({"type": "hello", "device_id": device_id}))
            print(
                f"[daemon] connected. Open {args.broker} in a browser to see incoming "
                "dispatches and respond to approval prompts."
            )
            await handle_broker(ws, state, workspace, private_key)
    except websockets.InvalidStatus as e:
        print(f"[daemon] handshake failed: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"[daemon] could not reach broker: {e}", file=sys.stderr)
        return 4
    return 0


def _paths_in_input(tool_name: str, tool_input: dict) -> list[str]:
    out = []
    for arg in _PATH_ARGS.get(tool_name, ()):
        value = tool_input.get(arg)
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _path_allowed(raw: str, allowed_dirs: list[Path]) -> bool:
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for base in allowed_dirs:
        try:
            resolved.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def verify_inbound(
    payload: DispatchPayload, signing: dict | None, state: DaemonState
) -> tuple[bool, str]:
    """Layer 2 verification of an incoming dispatch.

    Checks, in order: a signing block is present and complete; the
    dispatch is fresh (signed within FRESHNESS_WINDOW_S); the nonce
    hasn't been seen; the sender device's public key matches the locally
    pinned key (TOFU — first sighting pins it); the Ed25519 signature is
    valid. Returns (ok, reason).
    """
    if not signing:
        return False, "unsigned dispatch"
    sender_device = signing.get("sender_device")
    nonce = signing.get("nonce")
    signature_b64 = signing.get("signature")
    pubkey_b64 = signing.get("sender_public_key")
    created_at = signing.get("created_at")
    if not all((sender_device, nonce, signature_b64, pubkey_b64, created_at)):
        return False, "incomplete signing block"

    # Freshness.
    try:
        signed_at = datetime.fromisoformat(created_at)
    except ValueError:
        return False, "unparseable created_at"
    age = (datetime.now(timezone.utc) - signed_at).total_seconds()
    if age > FRESHNESS_WINDOW_S:
        return False, f"stale dispatch ({int(age)}s old)"

    # Replay.
    if (sender_device, nonce) in state.seen_nonces:
        return False, "replayed nonce"

    # TOFU key pin — first sight pins the key; a later change is rejected.
    pins = load_pins()
    pinned = pins.get(sender_device)
    if pinned is None:
        pins[sender_device] = pubkey_b64
        save_pins(pins)
    elif pinned != pubkey_b64:
        return False, "sender device public key changed (possible key-swap)"

    # Signature over the canonical payload.
    canonical = canonical_dispatch_bytes(
        instruction=payload.task,
        sender_device=sender_device,
        recipient_user=payload.recipient_id,
        target_device=signing.get("target_device"),
        nonce=nonce,
        created_at=created_at,
    )
    if not crypto.verify(
        crypto.b64decode(pins[sender_device]),
        canonical,
        crypto.b64decode(signature_b64),
    ):
        return False, "invalid signature"

    state.seen_nonces.add((sender_device, nonce))
    return True, ""


async def handle_broker(
    ws: "websockets.WebSocketClientProtocol",
    state: DaemonState,
    workspace: Path,
    private_key: bytes,
) -> None:
    async def send_status(dispatch_id, status: DispatchStatus) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "dispatch_status",
                    "dispatch_id": str(dispatch_id),
                    "status": status.value,
                }
            )
        )

    async def send_event(dispatch_id, event: DispatchEvent) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "dispatch_event",
                    "dispatch_id": str(dispatch_id),
                    "event": event,
                }
            )
        )

    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")

        if mtype == "sign_request":
            # The broker is asking THIS device (the sender) to sign a
            # dispatch the user is sending.
            request_id = msg.get("request_id")
            try:
                canonical = canonical_dispatch_bytes(
                    instruction=msg["instruction"],
                    sender_device=msg["sender_device"],
                    recipient_user=msg["recipient_user"],
                    target_device=msg.get("target_device"),
                    nonce=msg["nonce"],
                    created_at=msg["created_at"],
                )
                signature = crypto.sign(private_key, canonical)
                await ws.send(
                    json.dumps(
                        {
                            "type": "signed",
                            "request_id": request_id,
                            "signature": crypto.b64encode(signature),
                        }
                    )
                )
            except Exception:
                logger.exception("failed to sign dispatch")

        elif mtype == "new_dispatch":
            try:
                payload = DispatchPayload(**msg["payload"])
            except Exception:
                logger.exception("rejecting malformed dispatch")
                continue
            ok, reason = verify_inbound(payload, msg.get("signing"), state)
            if not ok:
                print(
                    f"[daemon] REJECTED dispatch {str(payload.dispatch_id)[:8]}…: {reason}"
                )
                await send_status(payload.dispatch_id, DispatchStatus.failed)
                await send_event(
                    payload.dispatch_id,
                    {
                        "type": "error",
                        "data": {
                            "message": f"dispatch rejected by daemon: {reason}",
                            "exception": "SignatureRejected",
                        },
                    },
                )
                continue
            did = str(payload.dispatch_id)
            task = asyncio.create_task(
                process_dispatch(
                    payload, msg.get("scopes"), state, workspace,
                    send_status, send_event,
                )
            )
            state.running[did] = task
            task.add_done_callback(
                lambda _t, d=did: state.running.pop(d, None)
            )

        elif mtype == "cancel_dispatch":
            # The broker is telling us a trust edge was revoked — stop now.
            did = msg.get("dispatch_id")
            task = state.running.get(did)
            if task is not None and not task.done():
                task.cancel()
                print(f"[daemon] dispatch {str(did)[:8]}… cancelled (trust revoked)")

        elif mtype == "dispatch_decision":
            did = msg.get("dispatch_id")
            decision = msg.get("decision")
            fut = state.pending_decisions.get(did)
            if fut and not fut.done() and decision in ("accept", "reject"):
                fut.set_result(decision)

        elif mtype == "tool_approval":
            did = msg.get("dispatch_id")
            req_id = msg.get("request_id")
            decision = msg.get("decision")
            fut = state.pending_approvals.get((did, req_id))
            if fut and not fut.done() and decision in ("allow", "deny"):
                fut.set_result(decision)


async def process_dispatch(
    payload: DispatchPayload,
    scopes_data: dict | None,
    state: DaemonState,
    workspace: Path,
    send_status,
    send_event,
) -> None:
    dispatch_id = str(payload.dispatch_id)
    scope = Scopes(**(scopes_data or {}))
    print(
        f"[daemon] new dispatch {dispatch_id[:8]}… from {payload.sender_id}: "
        f"{payload.task!r} (tools={scope.tools}, approval={scope.approval})"
    )

    # Step 1 — top-level Accept / Reject.
    decision_fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending_decisions[dispatch_id] = decision_fut
    await send_status(payload.dispatch_id, DispatchStatus.delivered)

    try:
        top_decision = await asyncio.wait_for(decision_fut, timeout=DISPATCH_DECISION_TIMEOUT_S)
    except asyncio.TimeoutError:
        await send_status(payload.dispatch_id, DispatchStatus.expired)
        return
    finally:
        state.pending_decisions.pop(dispatch_id, None)

    if top_decision != "accept":
        await send_status(payload.dispatch_id, DispatchStatus.denied)
        return

    await send_status(payload.dispatch_id, DispatchStatus.accepted)

    # Step 2 — per-tool gating, scoped to the trust edge.
    scope_tools = set(scope.tools)
    paths_restricted = bool(scope.paths)
    allowed_dirs = [workspace.resolve()] + [
        Path(p).expanduser().resolve() for p in scope.paths
    ]

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
        # Tool must be in the edge's scope (the executor also disallows
        # out-of-scope tools — this is defense in depth).
        if tool_name not in scope_tools:
            return PermissionResultDeny(
                message=f"'{tool_name}' is outside the granted trust scope",
                interrupt=False,
            )

        # Path allowlist — only when the edge defines one.
        if paths_restricted:
            for raw in _paths_in_input(tool_name, tool_input):
                if not _path_allowed(raw, allowed_dirs):
                    await send_event(
                        payload.dispatch_id,
                        {
                            "type": "permission_response",
                            "data": {
                                "tool": tool_name,
                                "decision": "deny",
                                "reason": f"path '{raw}' outside scope",
                            },
                        },
                    )
                    return PermissionResultDeny(
                        message=f"path '{raw}' is outside the granted scope",
                        interrupt=False,
                    )

        # Approval mode.
        if scope.approval == "auto":
            return PermissionResultAllow(updated_input=tool_input)

        # Manual — every tool call goes to the recipient for approval.
        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        state.pending_approvals[(dispatch_id, request_id)] = fut
        await send_event(
            payload.dispatch_id,
            {
                "type": "permission_request",
                "data": {
                    "id": request_id,
                    "tool": tool_name,
                    "input": tool_input,
                },
            },
        )
        try:
            decision = await asyncio.wait_for(fut, timeout=TOOL_APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            state.pending_approvals.pop((dispatch_id, request_id), None)

        await send_event(
            payload.dispatch_id,
            {
                "type": "permission_response",
                "data": {"tool": tool_name, "decision": decision},
            },
        )
        if decision == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message="Recipient denied", interrupt=False)

    # Step 3 — actually run the agent, restricted to the edge's tools.
    await send_status(payload.dispatch_id, DispatchStatus.running)
    final = DispatchStatus.completed
    try:
        async for event in run_dispatch(
            payload,
            cwd=str(workspace),
            allowed_tools=list(scope.tools),
            can_use_tool=can_use_tool,
        ):
            await send_event(payload.dispatch_id, event)
            if event["type"] == "error":
                final = DispatchStatus.failed
    except Exception as exc:
        logger.exception("executor crashed")
        final = DispatchStatus.failed
        await send_event(
            payload.dispatch_id,
            {
                "type": "error",
                "data": {"message": str(exc), "exception": type(exc).__name__},
            },
        )

    await send_status(payload.dispatch_id, final)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_):
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        return loop.run_until_complete(run_session(args))
    except asyncio.CancelledError:
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
