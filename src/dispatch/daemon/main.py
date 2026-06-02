"""Recipient daemon — pure WebSocket client.

Usage:
  dispatch-daemon                       # uses saved config from ~/.dispatch/config.json
  dispatch-daemon --broker URL --token JWT   # explicit; also saves config for next time
  python -m dispatch.daemon ...         # equivalent if installed in a venv

What it does:
  - Connects to the broker via WebSocket as the authenticated user.
  - Serves a local FastAPI app on 127.0.0.1 — the ONLY surface that can
    resolve user-intent decisions (Accept/Reject + Allow/Deny). The broker
    WS no longer carries approval messages, so a compromised broker can
    never fabricate "user accepted" against this daemon.
  - For each `new_dispatch` from the broker:
      1. Verifies signature, freshness, replay, TOFU pin.
      2. Sends `dispatch_status: delivered` upstream, registers the
         dispatch in LocalState so the locally-served UI renders it.
      3. Waits for the user's Accept/Reject via the local API.
      4. If accepted, runs run_dispatch(). Per-tool permission prompts
         also resolve via the local API; out-of-scope calls auto-deny.
      5. Streams every executor event back to the broker so the sender
         can watch live, AND into LocalState for the recipient's local UI.

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
from dispatch.daemon.nonces import NonceStore
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
DEFAULT_WORKSPACE = Path.home() / "dispatch" / "workspace"
DISPATCH_DECISION_TIMEOUT_S = 300.0     # how long the recipient has to accept/reject
TOOL_APPROVAL_TIMEOUT_S = 120.0          # how long they have to allow/deny one tool call

# Async/offline delivery: a dispatch may sit queued for an offline recipient
# for a long time, so the signature-freshness window is widened from minutes
# to a long retention horizon. Replay is caught by the *durable* nonce store
# (NonceStore), not by this clock — the window is now just an upper bound on
# how stale a delivered dispatch may be. The nonce store retains accepted
# pairs for the SAME horizon, so a dispatch can never outlive its own nonce
# record (which would make it replayable). Keep the broker's max dispatch
# expiry (schema.DispatchCreateRequest) <= this value to preserve that.
OFFLINE_RETENTION_S = 30 * 24 * 3600.0   # 30 days
FRESHNESS_WINDOW_S = OFFLINE_RETENTION_S  # reject dispatches signed > this ago
NONCE_PRUNE_INTERVAL_S = 6 * 3600.0      # prune the replay-guard store every 6h

logger = logging.getLogger("dispatch.daemon")


class SignedOutByBroker(Exception):
    """Raised when the broker tells us the user signed out from the web UI.

    The supervisor catches this to stop the reconnect loop instead of
    treating it like a normal disconnect.
    """


def _evict_port(port: int) -> None:
    """Make port `port` bindable.

    There are two reasons it might be taken when we get here:
      1. A zombie process from a previous app session — kill it via lsof+SIGKILL.
      2. The previous daemon thread in THIS process just stopped, but its socket
         is still in the kernel's TIME_WAIT / lingering close state. We can't
         kill ourselves; we just wait for the kernel to release the binding.
    """
    import subprocess
    import socket as _s
    import time

    def port_free() -> bool:
        s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        try:
            s.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if port_free():
        return

    # Try lsof in the common macOS paths — the bundled .app has a minimal PATH.
    for lsof in ("/usr/sbin/lsof", "/usr/bin/lsof", "lsof"):
        try:
            result = subprocess.run(
                [lsof, "-ti", f":{port}"],
                capture_output=True, text=True, timeout=3,
            )
            for pid_str in result.stdout.strip().splitlines():
                try:
                    pid = int(pid_str)
                    if pid != os.getpid():
                        os.kill(pid, signal.SIGKILL)
                        print(f"[daemon] killed pid={pid} on port {port}", flush=True)
                except (ValueError, ProcessLookupError, OSError):
                    pass
            break
        except (FileNotFoundError, subprocess.SubprocessError):
            continue

    # Wait up to 8s for the kernel to actually free the port — covers both
    # "the holder we just killed" and "the previous in-process server's TIME_WAIT".
    for i in range(80):
        if port_free():
            if i:
                print(f"[daemon] port {port} freed after {i*100}ms", flush=True)
            return
        time.sleep(0.1)
    print(f"[daemon] WARNING: port {port} still busy after 8s", flush=True)


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
    # Durable replay guard: accepted (sender_device, nonce) pairs survive
    # daemon restarts (see nonces.NonceStore). Set in run_session.
    nonce_store: NonceStore | None = None
    # Ask-on-first-use MCP grants: sender_id -> set of MCP server names the
    # recipient approved for that sender THIS session. Lets the recipient grant
    # a server once (on first request) instead of curating up front; cleared on
    # restart (durable per-edge persistence is a follow-up).
    session_mcp_grants: dict[str, set] = field(default_factory=dict)
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
        help="Working directory the agent operates in. Default: ~/dispatch/workspace",
    )
    parser.add_argument(
        "--anthropic-key",
        default=os.environ.get("ANTHROPIC_API_KEY") or config.get("anthropic_api_key"),
        help=(
            "Anthropic API key the agent uses. "
            "Saved to ~/.dispatch/config.json on first use — "
            "bare `dispatch-daemon` reads it on later runs."
        ),
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=int(os.environ.get("DISPATCH_LOCAL_PORT", config.get("local_port") or 8001)),
        help="Port for the local approval UI on 127.0.0.1. Default: 8001.",
    )
    return parser.parse_args(argv)


async def run_session(
    args: argparse.Namespace, on_status=None, on_notification=None, on_signout=None,
) -> int:
    """Run a single broker session.

    on_status: optional callback called with one of
        "enrolling" | "connecting" | "connected" | "disconnected"
    so a supervisor (e.g. the tray app) can show live status.

    on_notification: optional callback `(title, subtitle, message)` invoked
    on user-visible events (incoming dispatch, tool needing approval).
    The tray app supplies this to post a macOS UserNotification.
    """
    def _emit(state: str) -> None:
        if on_status is not None:
            try:
                on_status(state)
            except Exception:
                pass

    if not args.token:
        print(
            "error: no token. Sign in on the broker's web page, then either:\n"
            "  - run the one-line installer it shows you, or\n"
            "  - run: dispatch-daemon --broker <url> --token <jwt>",
            file=sys.stderr,
        )
        return 2

    _emit("enrolling")
    print(f"[daemon] >>> begin enrollment, broker={args.broker}", flush=True)

    # Device enrollment: ensure this machine has an Ed25519 keypair and a
    # broker-issued device_id. Generates the keypair on first run.
    try:
        device_id = await asyncio.wait_for(
            ensure_enrolled(
                args.broker, args.token, _load_config().get("device_id")
            ),
            timeout=15.0,
        )
        print(f"[daemon] enrollment OK -> {device_id}", flush=True)
    except asyncio.TimeoutError:
        print("[daemon] enrollment TIMED OUT after 15s", file=sys.stderr, flush=True)
        return 5
    except Exception as exc:
        print(f"[daemon] device enrollment FAILED: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        import traceback; traceback.print_exc(file=sys.stderr)
        return 5

    # If the user passed --anthropic-key (or env / saved config supplied one),
    # make sure the agent SDK sees it AND it gets persisted so future bare
    # `dispatch-daemon` runs Just Work.
    if args.anthropic_key:
        os.environ["ANTHROPIC_API_KEY"] = args.anthropic_key

    # Remember broker + token + device + api key so future runs can be a
    # bare `dispatch-daemon`. (None values are skipped by _save_config.)
    _save_config(
        broker=args.broker,
        token=args.token,
        device_id=device_id,
        anthropic_api_key=args.anthropic_key,
    )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY is not set. Set it once with:\n"
            "    dispatch-daemon --anthropic-key sk-ant-...\n"
            "or export it in your shell. Without it the agent will fail to run.",
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

    # Durable replay guard. Persisting accepted (sender_device, nonce) pairs
    # across restarts lets us widen the freshness window to a long horizon so
    # dispatches can wait for an offline recipient — without ever re-running a
    # replayed dispatch. Prune on startup so the table stays bounded.
    my_user = verify_token_user(args.token)
    nonce_store = NonceStore(dispatch_home() / "nonces.db", FRESHNESS_WINDOW_S)
    try:
        nonce_store.prune(datetime.now(timezone.utc).timestamp())
    except Exception:
        logger.exception("nonce store prune failed (continuing)")
    state.nonce_store = nonce_store

    async def _prune_nonces_periodically() -> None:
        while True:
            await asyncio.sleep(NONCE_PRUNE_INTERVAL_S)
            try:
                nonce_store.prune(datetime.now(timezone.utc).timestamp())
            except Exception:
                logger.exception("periodic nonce prune failed")

    prune_task = asyncio.create_task(_prune_nonces_periodically())

    # Local approval UI — the ONLY surface that resolves user-intent
    # decisions. The broker's WS no longer carries them.
    from dispatch.daemon.local_app import LocalState, issue_local_token, spawn as spawn_local_ui
    local_state = LocalState(
        user_id=my_user,
        broker_url=args.broker,
        broker_token=args.token,
        notify=on_notification,
        on_signout=on_signout,
    )
    local_port = int(_load_config().get("local_port") or args.local_port or 8001)
    print(f"[daemon] evicting any process on port {local_port}", flush=True)
    _evict_port(local_port)  # kill any stale process holding our port
    local_token = issue_local_token()

    # Workflow engine lives alongside the local UI so the routes mounted
    # on the FastAPI app and the broker WS handler both see the same
    # instance. The broker WS pushes workflow_run_start frames; the engine
    # walks the graph and PATCHes run state back to the broker.
    from dispatch.daemon.workflows import WorkflowEngine
    workflow_engine = WorkflowEngine(
        local_state=local_state,
        broker_url=args.broker,
        broker_token=args.token,
    )

    from dispatch.daemon.workflow_scheduler import CronScheduler
    scheduler = CronScheduler(
        workflow_engine,
        args.broker,
        lambda: args.token,
    )
    await scheduler.start()

    print(f"[daemon] spawning local UI server on port {local_port}", flush=True)
    local_server = spawn_local_ui(
        local_state, state, local_token,
        port=local_port,
        workflow_engine=workflow_engine,
    )
    # Wait until uvicorn actually accepts a connection — otherwise the tray's
    # "Open Inbox" probe races the bind and shows the not-responding alert.
    import socket as _s
    for i in range(40):  # up to 4 s
        await asyncio.sleep(0.1)
        sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", local_port))
            sock.close()
            print(f"[daemon] local UI bound after {i*100}ms", flush=True)
            break
        except (_s.timeout, OSError):
            sock.close()
    else:
        print(f"[daemon] WARNING: local UI never accepted on port {local_port}",
              file=sys.stderr, flush=True)
    print(f"[daemon] local UI: http://127.0.0.1:{local_port}?t=<see ~/.dispatch/local.token>", flush=True)

    # Populate the inbox from the broker DB so past dispatches are visible
    # immediately after a restart, before any new ones arrive over the WS.
    await local_state.seed_from_broker()

    ws_url = _broker_ws_url(args.broker, args.token)
    ssl_ctx = _ssl_context_for(ws_url)
    print(f"[daemon] connecting to broker: {args.broker}")
    _emit("connecting")
    try:
        async with websockets.connect(ws_url, max_size=None, ssl=ssl_ctx) as ws:
            # First frame identifies this device to the broker.
            await ws.send(json.dumps({"type": "hello", "device_id": device_id}))
            print(
                f"[daemon] connected. Open http://127.0.0.1:{local_port} to approve dispatches."
            )
            _emit("connected")
            await handle_broker(
                ws, state, workspace, private_key,
                local_state=local_state,
                workflow_engine=workflow_engine,
                my_user=my_user,
                my_device=device_id,
            )
    except websockets.InvalidStatus as e:
        # 401/403 means the JWT is revoked or otherwise rejected by the
        # broker — treat it the same as a signed_out push and stop trying.
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code in (401, 403):
            print(f"[daemon] broker rejected our token ({status_code}) — signed out", flush=True)
            try:
                cfg = _load_config()
                cfg.pop("token", None)
                _config_path().write_text(json.dumps(cfg, indent=2))
                _config_path().chmod(0o600)
            except Exception:
                logger.exception("failed to clear token after auth rejection")
            return 7
        print(f"[daemon] handshake failed: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"[daemon] could not reach broker: {e}", file=sys.stderr)
        return 4
    except SignedOutByBroker:
        # Special exit code so the supervisor stops retrying instead of
        # reconnecting with a now-cleared token.
        return 7
    finally:
        # Close every open WebSocket on the local UI server so uvicorn can
        # shut down immediately. Without this, the browser's /ws/events
        # connection keeps uvicorn alive past its graceful timeout, the port
        # stays bound, and the retry loop fails to rebind.
        for ws in list(local_state.watchers):
            try:
                await ws.close()
            except Exception:
                pass
        local_state.watchers.clear()
        await scheduler.stop()
        await local_server.stop()
        prune_task.cancel()
        try:
            await prune_task
        except (asyncio.CancelledError, Exception):
            pass
        nonce_store.close()
    return 0


def verify_token_user(jwt_token: str) -> str:
    """Best-effort: pull the email out of the daemon's broker JWT so the
    local UI can show 'signed in as X'. Decodes without verification — the
    broker is the authority; this is purely cosmetic."""
    try:
        import base64
        parts = jwt_token.split(".")
        if len(parts) != 3:
            return ""
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        return claims.get("sub") or ""
    except Exception:
        return ""


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


def _is_dispatch_control_tool(tool_name: str) -> bool:
    """The dispatch control plane (send / invite / accept / approve / cancel).

    A *delegated task* must never invoke these, regardless of the edge scope
    or approval mode — otherwise task text (benign-ambiguous like 'to edward',
    or adversarial via prompt injection) could re-wield the recipient's
    identity to dispatch onward. Withheld structurally (capability removal),
    not by a system prompt."""
    n = tool_name.lower()
    return n.startswith("mcp__dispatch__") or n == "dispatch" or n.startswith("dispatch_")


def _mcp_server_of(tool_name: str) -> str:
    """Extract the server name from an MCP tool: mcp__<server>__<tool>."""
    parts = tool_name.split("__")
    return parts[1] if tool_name.startswith("mcp__") and len(parts) >= 3 else ""


def _shareable_mcp_path() -> Path:
    return dispatch_home() / "shareable-mcp.json"


def load_shareable_mcp() -> dict:
    """The recipient's pool of MCP servers exposable to incoming dispatches.

    Curated once in ~/.dispatch/shareable-mcp.json — either a {name: config}
    map or {"mcpServers": {...}} (same shape as a Claude .mcp.json). This is
    the *only* set a dispatch can ever reach; the dispatch control plane is
    never shareable, so any 'dispatch' entry is dropped. Empty by default
    (no MCP exposed until the recipient opts in)."""
    try:
        raw = json.loads(_shareable_mcp_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        return {}
    return {k: v for k, v in servers.items() if k.lower() != "dispatch"}


def _mcp_tool_allowed(tool_name: str, patterns: list[str]) -> bool:
    """Does an MCP tool (`mcp__<server>__<tool>`) match the edge's `mcp`
    allowlist? Supports an exact tool name, a trailing-`*` prefix, or a bare
    server name (= any tool from that server)."""
    for pat in patterns:
        p = (pat or "").strip()
        if not p:
            continue
        if p == tool_name:
            return True
        if p.endswith("*") and tool_name.startswith(p[:-1]):
            return True
        if "__" not in p and tool_name.startswith(f"mcp__{p}__"):
            return True
    return False


def verify_inbound(
    payload: DispatchPayload,
    signing: dict | None,
    state: DaemonState,
    my_user: str | None = None,
    my_device: str | None = None,
) -> tuple[bool, str]:
    """Layer 2 verification of an incoming dispatch.

    Checks, in order: a signing block is present and complete; the
    dispatch is actually addressed to this user (and this device, when
    pinned to one); it is fresh (signed within FRESHNESS_WINDOW_S); the
    (sender_device, nonce) pair hasn't been seen before (durable replay
    guard); the sender device's public key matches the locally pinned key
    (TOFU — first sighting pins it); the Ed25519 signature is valid.
    Returns (ok, reason).

    The nonce is only recorded as seen *after* every check passes, so a
    dispatch that fails verification doesn't burn a nonce.
    """
    if not signing:
        return False, "unsigned dispatch"
    sender_device = signing.get("sender_device")
    nonce = signing.get("nonce")
    signature_b64 = signing.get("signature")
    pubkey_b64 = signing.get("sender_public_key")
    created_at = signing.get("created_at")
    target_device = signing.get("target_device")
    if not all((sender_device, nonce, signature_b64, pubkey_b64, created_at)):
        return False, "incomplete signing block"

    # Addressing. The signature covers recipient_user and target_device, so
    # these can't be rewritten in flight — but the daemon must still refuse
    # a dispatch the broker misrouted (or replayed) to the wrong user or a
    # sibling device. recipient_user binds to a person; target_device, when
    # the sender pins one, binds to a single machine so a dispatch can't be
    # re-run on another of the same user's devices. A null target_device
    # means "any of my devices" (today's default) and is accepted.
    if my_user is not None and payload.recipient_id != my_user:
        return False, f"misaddressed dispatch (for {payload.recipient_id}, not {my_user})"
    if target_device is not None and my_device is not None and target_device != my_device:
        return False, "dispatch targeted at a different device"

    # Freshness.
    try:
        signed_at = datetime.fromisoformat(created_at)
    except ValueError:
        return False, "unparseable created_at"
    age = (datetime.now(timezone.utc) - signed_at).total_seconds()
    if age > FRESHNESS_WINDOW_S:
        return False, f"stale dispatch ({int(age)}s old)"

    # Replay — durable across daemon restarts.
    if state.nonce_store is not None and state.nonce_store.seen(sender_device, nonce):
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
        target_device=target_device,
        nonce=nonce,
        created_at=created_at,
    )
    if not crypto.verify(
        crypto.b64decode(pins[sender_device]),
        canonical,
        crypto.b64decode(signature_b64),
    ):
        return False, "invalid signature"

    if state.nonce_store is not None:
        state.nonce_store.record(sender_device, nonce, datetime.now(timezone.utc).timestamp())
    return True, ""


async def handle_broker(
    ws: "websockets.WebSocketClientProtocol",
    state: DaemonState,
    workspace: Path,
    private_key: bytes,
    local_state=None,
    workflow_engine=None,
    my_user: str | None = None,
    my_device: str | None = None,
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

        if mtype == "error":
            # Broker is telling us why it's about to close (e.g. unknown
            # device, revoked, replaced by another connection). Surface it
            # so users can see why their daemon keeps reconnecting.
            data = msg.get("data", {}) or {}
            print(
                f"[daemon] broker error: {data.get('exception', 'Error')}: "
                f"{data.get('message', raw)}",
                file=sys.stderr,
            )
            continue

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
            ok, reason = verify_inbound(
                payload, msg.get("signing"), state,
                my_user=my_user, my_device=my_device,
            )
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
            if local_state is not None:
                local_state.on_new_dispatch(payload, msg.get("scopes"))
            task = asyncio.create_task(
                process_dispatch(
                    payload, msg.get("scopes"), state, workspace,
                    send_status, send_event,
                    local_state=local_state,
                    workflow_engine=workflow_engine,
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

        elif mtype == "signed_out":
            # The user signed out from the broker's web page. Clear the
            # cached broker JWT from disk and propagate the signal upward
            # so the supervisor stops the session without reconnecting.
            print("[daemon] broker signaled sign-out — clearing local credentials")
            try:
                cfg = _load_config()
                cfg.pop("token", None)
                _config_path().write_text(json.dumps(cfg, indent=2))
                _config_path().chmod(0o600)
            except Exception:
                logger.exception("failed to clear token on sign-out")
            raise SignedOutByBroker()

        # Approval decisions (top-level Accept/Reject + per-tool Allow/Deny)
        # are now resolved ONLY by the locally-served UI talking to the
        # daemon's own HTTP server. The daemon ignores any such message
        # arriving over the broker WS — a compromised broker must not be
        # able to fabricate user intent.


async def process_dispatch(
    payload: DispatchPayload,
    scopes_data: dict | None,
    state: DaemonState,
    workspace: Path,
    send_status,
    send_event,
    local_state=None,
    workflow_engine=None,
) -> None:
    dispatch_id = str(payload.dispatch_id)
    scope = Scopes(**(scopes_data or {}))
    print(
        f"[daemon] new dispatch {dispatch_id[:8]}… from {payload.sender_id}: "
        f"{payload.task!r} (tools={scope.tools}, approval={scope.approval})"
    )

    # Mirror status + events into the local UI so the recipient sees live
    # progress. The broker still gets the same updates for the sender's
    # /watch view — these wrappers are additive.
    if local_state is not None:
        _orig_send_status = send_status
        _orig_send_event = send_event

        async def send_status(dispatch_id_, status):  # noqa: F811
            local_state.on_status(dispatch_id_, status)
            await _orig_send_status(dispatch_id_, status)

        async def send_event(dispatch_id_, event):  # noqa: F811
            local_state.on_event(dispatch_id_, event)
            await _orig_send_event(dispatch_id_, event)

    # n8n-style: workflows execute the moment they arrive. The trust
    # edge already gates which senders may dispatch to this device and
    # which tools their agents may use — making the recipient click
    # Accept on every workflow defeats the "automation" purpose. For
    # single-prompt dispatches we still require Accept so a teammate
    # can opt out of any one ad-hoc task.
    is_workflow = bool((payload.metadata or {}).get("workflow"))

    if is_workflow:
        # Skip the Accept/Reject decision; jump straight to accepted.
        # The recipient still sees the dispatch in their local UI
        # immediately so they can watch (and Cancel) the auto-run.
        await send_status(payload.dispatch_id, DispatchStatus.delivered)
        await send_status(payload.dispatch_id, DispatchStatus.accepted)
    else:
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
    scope_mcp = list(scope.mcp or [])
    paths_restricted = bool(scope.paths)
    allowed_dirs = [workspace.resolve()] + [
        Path(p).expanduser().resolve() for p in scope.paths
    ]
    # The recipient's pool of MCP servers exposable to dispatches. A delegated
    # task can only ever reach servers in this pool (it's what we hand the
    # executor); within it a server becomes usable for this sender once granted
    # — pre-granted in the edge's `mcp` scope, or approved on first use (then
    # remembered for the session). No up-front curation required.
    mcp_pool = load_shareable_mcp()

    async def _request_approval(tool_name: str, tool_input: dict[str, Any]) -> str:
        """Surface one tool call to the recipient (Layer 3); await allow/deny
        (timeout → deny)."""
        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        state.pending_approvals[(dispatch_id, request_id)] = fut
        if local_state is not None:
            local_state.on_pending_tool(payload.dispatch_id, request_id, tool_name, tool_input)
        await send_event(
            payload.dispatch_id,
            {"type": "permission_request",
             "data": {"id": request_id, "tool": tool_name, "input": tool_input}},
        )
        try:
            decision = await asyncio.wait_for(fut, timeout=TOOL_APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            state.pending_approvals.pop((dispatch_id, request_id), None)
            if local_state is not None:
                local_state.on_tool_resolved(payload.dispatch_id, request_id)
        await send_event(
            payload.dispatch_id,
            {"type": "permission_response", "data": {"tool": tool_name, "decision": decision}},
        )
        return decision

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
        # (a) Self-delegation / authority — NEVER allowed in a delegated task,
        # independent of scope or approval mode. This is the structural fix for
        # task text re-wielding the recipient's identity (the "to edward"
        # hijack): you can't call a tool you don't have. Robust to prompt
        # injection in a way a system prompt is not.
        if _is_dispatch_control_tool(tool_name):
            await send_event(
                payload.dispatch_id,
                {"type": "permission_response",
                 "data": {"tool": tool_name, "decision": "deny",
                          "reason": "dispatch control plane withheld from delegated tasks"}},
            )
            return PermissionResultDeny(
                message="a delegated task cannot send, relay, or re-dispatch",
                interrupt=False,
            )

        # (b) Capability check. Built-ins via the edge's `tools`. MCP tools via
        # the edge's `mcp` allowlist OR a session grant — and if neither, a
        # first use of a shareable-pool server prompts the recipient to grant
        # it once for this sender (then it's remembered).
        if tool_name.startswith("mcp__"):
            server = _mcp_server_of(tool_name)
            granted = _mcp_tool_allowed(tool_name, scope_mcp) or (
                server in state.session_mcp_grants.get(payload.sender_id, set())
            )
            if not granted:
                if not server or server not in mcp_pool:
                    return PermissionResultDeny(
                        message=f"MCP server '{server}' is not in your shareable pool",
                        interrupt=False,
                    )
                # First use → ask the recipient to grant this server (once).
                decision = await _request_approval(tool_name, tool_input)
                if decision != "allow":
                    return PermissionResultDeny(
                        message=f"recipient did not grant MCP server '{server}'",
                        interrupt=False,
                    )
                state.session_mcp_grants.setdefault(payload.sender_id, set()).add(server)
                return PermissionResultAllow(updated_input=tool_input)
            # granted → fall through to path + approval-mode handling
        elif tool_name not in scope_tools:
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

        # (d) Approval mode for an already-permitted tool. Workflows + `auto`
        # edges run unattended; `manual` edges approve every call (Layer 3).
        if is_workflow or scope.approval == "auto":
            return PermissionResultAllow(updated_input=tool_input)
        decision = await _request_approval(tool_name, tool_input)
        if decision == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message="Recipient denied", interrupt=False)

    # Step 3 — run the payload.
    #
    # Two flavors:
    #   - A regular single-prompt dispatch → run_dispatch() generator.
    #   - A workflow dispatch (metadata.workflow present) → hand the
    #     definition to the WorkflowEngine, which walks the graph using
    #     the same can_use_tool callback for every agent node so tool
    #     scopes / approvals stay identical to a single-prompt dispatch.
    await send_status(payload.dispatch_id, DispatchStatus.running)
    final = DispatchStatus.completed

    envelope = (payload.metadata or {}).get("workflow")
    if envelope and workflow_engine is not None:
        try:
            from uuid import UUID as _UUID
            run_id = _UUID(envelope["run_id"])
            definition = envelope.get("definition") or {"nodes": [], "edges": []}
            wf_input = envelope.get("input") or {}
        except (KeyError, ValueError) as exc:
            await send_event(
                payload.dispatch_id,
                {
                    "type": "error",
                    "data": {"message": f"bad workflow envelope: {exc}", "exception": "BadEnvelope"},
                },
            )
            await send_status(payload.dispatch_id, DispatchStatus.failed)
            return

        try:
            run_status = await workflow_engine.run_for_dispatch(
                run_id=run_id,
                definition=definition,
                input_=wf_input,
                workspace=workspace,
                allowed_tools=list(scope.tools),
                can_use_tool=can_use_tool,
                sender_id=payload.sender_id,
                send_event=send_event,
            )
        except asyncio.CancelledError:
            await send_status(payload.dispatch_id, DispatchStatus.cancelled)
            raise
        except Exception as exc:
            logger.exception("workflow engine crashed")
            await send_event(
                payload.dispatch_id,
                {
                    "type": "error",
                    "data": {"message": str(exc), "exception": type(exc).__name__},
                },
            )
            await send_status(payload.dispatch_id, DispatchStatus.failed)
            return

        # Map workflow terminal status onto the parent dispatch's status
        # so the sender's sent-list / detail page reflects the outcome.
        if run_status.value == "completed":
            final = DispatchStatus.completed
        elif run_status.value == "cancelled":
            final = DispatchStatus.cancelled
        else:
            final = DispatchStatus.failed
        await send_status(payload.dispatch_id, final)
        return

    try:
        async for event in run_dispatch(
            payload,
            cwd=str(workspace),
            allowed_tools=list(scope.tools),
            can_use_tool=can_use_tool,
            mcp_servers=mcp_pool or None,
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
