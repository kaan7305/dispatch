"""`dispatch` — a thin terminal client for the Dispatch broker.

This is the command surface the Claude Code `dispatch` skill drives, the
same way Beeper's skill drives its `beeper` CLI. It does **not** replace the
daemon or the web UI — it talks to the broker's HTTP API (and the `/inbox`
WebSocket for accept/decline) using the broker URL + JWT that the daemon
already saved to ``~/.dispatch/config.json``.

Resolution order for broker/token (matches the daemon):
    CLI flag  >  $DISPATCH_BROKER / $DISPATCH_TOKEN  >  ~/.dispatch/config.json
    >  http://localhost:8000 (broker default; no token default)

Commands:
    dispatch whoami                         GET  /me
    dispatch contacts                       GET  /trust
    dispatch send <to> '<task>' [...]       POST /dispatch
    dispatch sent                           GET  /dispatches?role=sent
    dispatch inbox                          GET  /dispatches?role=received
    dispatch status <id>                    GET  /dispatch/{id}
    dispatch accept <id>                    WS   /inbox  dispatch_decision=accept
    dispatch decline <id>                   WS   /inbox  dispatch_decision=reject
    dispatch cancel <id>                    POST /dispatch/{id}/cancel

Every command takes ``--json`` for machine-readable output (what the skill
parses) and ``--broker`` / ``--token`` overrides.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import certifi
import httpx

# WebSocket client is only needed for accept/decline; import lazily there so
# the common HTTP commands stay fast and don't hard-require it at import time.


HTTP_TIMEOUT_S = 30.0
# How long accept/decline waits on the inbox socket for the daemon to react
# before reporting "sent, awaiting daemon".
DECISION_CONFIRM_TIMEOUT_S = 8.0


# ----------------------------------------------------------------------------
# Config — shared with the daemon (~/.dispatch/config.json)
# ----------------------------------------------------------------------------


def _dispatch_home() -> Path:
    """Directory holding the daemon's config. Override with DISPATCH_HOME.

    Kept in sync with ``dispatch.daemon.identity.dispatch_home`` — duplicated
    here so the CLI doesn't import the daemon (and its keychain backend) just
    to resolve a path."""
    return Path(os.environ.get("DISPATCH_HOME", str(Path.home() / ".dispatch")))


def _config_path() -> Path:
    return _dispatch_home() / "config.json"


def _load_config() -> dict:
    try:
        return json.loads(_config_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _resolve_broker(arg: Optional[str], config: dict) -> str:
    broker = arg or os.environ.get("DISPATCH_BROKER") or config.get("broker") or "http://localhost:8000"
    return broker.rstrip("/")


def _resolve_token(arg: Optional[str], config: dict) -> Optional[str]:
    return arg or os.environ.get("DISPATCH_TOKEN") or config.get("token")


class CliError(Exception):
    """A user-facing error: printed to stderr, exit code 1, no traceback."""


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------


def _client(broker: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=broker,
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT_S,
        verify=certifi.where(),
    )


def _request(broker: str, token: str, method: str, path: str, **kw: Any) -> Any:
    """One HTTP call. Raises CliError with the broker's detail on 4xx/5xx."""
    try:
        with _client(broker, token) as c:
            resp = c.request(method, path, **kw)
    except httpx.ConnectError as e:
        raise CliError(
            f"can't reach broker at {broker} ({e}). Is it running / is the URL right?"
        )
    except httpx.HTTPError as e:
        raise CliError(f"request to {path} failed: {e}")

    if resp.status_code == 401:
        raise CliError(
            "broker rejected the token (401). It's missing or expired — "
            "sign in again and re-run the daemon installer to refresh "
            "~/.dispatch/config.json."
        )
    if resp.status_code >= 400:
        detail = _detail(resp)
        raise CliError(f"broker error {resp.status_code}: {detail}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return json.dumps(body)
    except ValueError:
        return resp.text.strip() or "(no body)"


# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------


def _emit(args: argparse.Namespace, payload: Any, human: str) -> None:
    """Print machine JSON when --json, otherwise the human string."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(human)


def _short(dispatch_id: str) -> str:
    return dispatch_id.split("-")[0]


def _fmt_dispatch_line(d: dict, who_key: str) -> str:
    task = d.get("task", "")
    if len(task) > 70:
        task = task[:67] + "…"
    return (
        f"  {_short(d['dispatch_id'])}  "
        f"[{d.get('status', '?'):<10}]  "
        f"{d.get(who_key, '?'):<28}  {task}"
    )


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------


def cmd_whoami(args: argparse.Namespace, broker: str, token: str) -> int:
    me = _request(broker, token, "GET", "/me")
    _emit(args, me, f"You are {me.get('user_id', '(unknown)')} on {broker}.")
    return 0


def cmd_contacts(args: argparse.Namespace, broker: str, token: str) -> int:
    data = _request(broker, token, "GET", "/trust")
    trust = data.get("trust", [])
    outgoing = [t for t in trust if t.get("direction") == "outgoing"]

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if not trust:
        print("No contacts yet. Invite someone (or accept an invite) in the web UI first.")
        return 0

    print("Contacts (who can send to whom):")
    for t in trust:
        arrow = "→ you can dispatch to" if t["direction"] == "outgoing" else "← can dispatch to you:"
        online = "online" if t.get("peer_online") else "offline"
        scopes = t.get("scopes", {})
        tools = ",".join(scopes.get("tools", [])) or "(default)"
        approval = scopes.get("approval", "?")
        print(f"  {arrow} {t['peer']:<28} [{online}]  tools={tools} approval={approval}")
    if not outgoing:
        print("\nNote: no outgoing edges — you can't send to anyone until a contact "
              "accepts your invitation and grants you scopes.")
    return 0


def cmd_send(args: argparse.Namespace, broker: str, token: str) -> int:
    metadata: dict[str, Any] = {}
    if args.cwd:
        metadata["cwd"] = args.cwd
    for kv in args.meta or []:
        if "=" not in kv:
            raise CliError(f"--meta expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        metadata[k] = v

    body = {
        "recipient_id": args.recipient,
        "task": args.task,
        "expires_in_seconds": args.expires,
        "metadata": metadata,
    }
    result = _request(broker, token, "POST", "/dispatch", json=body)

    # Broker flattens single-recipient success to {dispatch_id, status}.
    if "dispatch_id" in result:
        _emit(
            args,
            result,
            f"Sent. dispatch_id={result['dispatch_id']} status={result.get('status', '?')}\n"
            f"  Watch it:  dispatch status {result['dispatch_id']}",
        )
        return 0

    # Fan-out / failure shape.
    failures = result.get("failures", [])
    dispatches = result.get("dispatches", [])
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for d in dispatches:
            print(f"Sent to {d.get('recipient_id', '?')}: {d['dispatch_id']} ({d.get('status', '?')})")
        for f in failures:
            print(f"FAILED to {f.get('recipient_id', '?')}: {f.get('status_code')} {f.get('error')}")
    return 0 if dispatches and not failures else 1


def cmd_sent(args: argparse.Namespace, broker: str, token: str) -> int:
    return _list_dispatches(args, broker, token, role="sent", who_key="recipient_id", label="Sent")


def cmd_inbox(args: argparse.Namespace, broker: str, token: str) -> int:
    return _list_dispatches(args, broker, token, role="received", who_key="sender_id", label="Inbox")


def _list_dispatches(
    args: argparse.Namespace, broker: str, token: str, *, role: str, who_key: str, label: str
) -> int:
    data = _request(broker, token, "GET", "/dispatches", params={"role": role})
    items = data.get("dispatches", [])
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if not items:
        print(f"{label}: empty.")
        return 0
    header = "from" if role == "received" else "to"
    print(f"{label} ({len(items)}):  id        [status]      {header}")
    for d in items:
        print(_fmt_dispatch_line(d, who_key))
    return 0


def cmd_status(args: argparse.Namespace, broker: str, token: str) -> int:
    d = _request(broker, token, "GET", f"/dispatch/{args.dispatch_id}")
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    print(f"dispatch {d['dispatch_id']}")
    print(f"  from:    {d.get('sender_id')}")
    print(f"  to:      {d.get('recipient_id')}")
    print(f"  status:  {d.get('status')}")
    print(f"  created: {d.get('created_at')}")
    print(f"  expires: {d.get('expires_at')}")
    print(f"  task:    {d.get('task')}")
    events = d.get("events", [])
    if events:
        print(f"  events ({len(events)}):")
        for e in events:
            etype = e.get("type", "?")
            edata = e.get("data", e)
            print(f"    - {etype}: {json.dumps(edata) if not isinstance(edata, str) else edata}")
    return 0


def cmd_cancel(args: argparse.Namespace, broker: str, token: str) -> int:
    result = _request(broker, token, "POST", f"/dispatch/{args.dispatch_id}/cancel")
    status = result.get("status", "?")
    if status == "noop":
        _emit(args, result, f"Already terminal ({result.get('current_status')}); nothing to cancel.")
    else:
        _emit(args, result, f"Cancelled {args.dispatch_id}.")
    return 0


def cmd_accept(args: argparse.Namespace, broker: str, token: str) -> int:
    return asyncio.run(_decide(args, broker, token, decision="accept"))


def cmd_decline(args: argparse.Namespace, broker: str, token: str) -> int:
    return asyncio.run(_decide(args, broker, token, decision="reject"))


# ----------------------------------------------------------------------------
# accept / decline over the /inbox WebSocket
# ----------------------------------------------------------------------------


def _inbox_ws_url(broker: str, token: str) -> str:
    p = urlparse(broker)
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, "/inbox", "", f"token={token}", ""))


async def _decide(args: argparse.Namespace, broker: str, token: str, *, decision: str) -> int:
    """Send a dispatch_decision over the inbox socket.

    The broker forwards the decision to the recipient's daemon. The daemon
    must be online for it to take effect; if it isn't, the broker silently
    drops it (by design). We connect, confirm the dispatch is in our received
    inbox snapshot, send the decision, then wait briefly for a status event
    to confirm the daemon acted.
    """
    import ssl

    import websockets

    target = args.dispatch_id
    url = _inbox_ws_url(broker, token)
    ssl_ctx = ssl.create_default_context(cafile=certifi.where()) if url.startswith("wss://") else None

    seen_in_inbox = False
    confirmed: Optional[str] = None
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=HTTP_TIMEOUT_S) as ws:
            # The broker replays the inbox snapshot on connect. Drain it briefly
            # to confirm `target` is actually addressed to us before deciding.
            try:
                async with asyncio.timeout(5.0):
                    while not seen_in_inbox:
                        msg = json.loads(await ws.recv())
                        if msg.get("type") == "inbox_new":
                            if str(msg.get("data", {}).get("dispatch_id")) == target:
                                seen_in_inbox = True
            except (asyncio.TimeoutError, TimeoutError):
                pass

            if not seen_in_inbox:
                raise CliError(
                    f"dispatch {target} is not in your inbox. Run `dispatch inbox` "
                    "to list what's actually addressed to you (check the full id)."
                )

            await ws.send(json.dumps(
                {"type": "dispatch_decision", "dispatch_id": target, "decision": decision}
            ))

            # Wait for a status/event echo on this dispatch as confirmation the
            # daemon picked it up.
            try:
                async with asyncio.timeout(DECISION_CONFIRM_TIMEOUT_S):
                    while confirmed is None:
                        msg = json.loads(await ws.recv())
                        if str(msg.get("dispatch_id")) == target:
                            confirmed = msg.get("type") or msg.get("data", {}).get("status") or "event"
            except (asyncio.TimeoutError, TimeoutError):
                pass
    except CliError:
        raise
    except Exception as e:  # connection / protocol errors
        raise CliError(f"inbox socket failed: {e}")

    verb = "Accepted" if decision == "accept" else "Declined"
    payload = {"dispatch_id": target, "decision": decision, "confirmed": confirmed is not None}
    if confirmed:
        _emit(args, payload, f"{verb} {_short(target)}. Daemon acknowledged ({confirmed}).")
    else:
        _emit(
            args, payload,
            f"{verb} {_short(target)} — decision sent. No echo yet; if nothing happens, "
            "your daemon may be offline. Start `dispatch-daemon` and re-run.",
        )
    return 0


# ----------------------------------------------------------------------------
# argparse
# ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Connection/output flags live on a shared parent so they're accepted
    # both BEFORE the subcommand (`dispatch --json contacts`) and AFTER it
    # (`dispatch contacts --json`). SUPPRESS defaults mean an absent flag in
    # one position never clobbers a value supplied in the other; main() reads
    # them with getattr fallbacks.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--broker", default=argparse.SUPPRESS,
        help="Broker base URL. Default: $DISPATCH_BROKER, then ~/.dispatch/config.json, then localhost:8000.",
    )
    common.add_argument(
        "--token", default=argparse.SUPPRESS,
        help="JWT bearer token. Default: $DISPATCH_TOKEN, then ~/.dispatch/config.json.",
    )
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="Machine-readable JSON output.",
    )

    parser = argparse.ArgumentParser(
        prog="dispatch",
        description="Terminal client for the Dispatch broker (drives the /dispatch Claude skill).",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help: str, func) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, parents=[common])
        p.set_defaults(func=func)
        return p

    add("whoami", "Show the signed-in user + broker.", cmd_whoami)
    add("contacts", "List trust edges (who can dispatch to whom).", cmd_contacts)

    p_send = add("send", "Send a dispatch (your daemon must be online to sign).", cmd_send)
    p_send.add_argument("recipient", help="Recipient user id (their email/identifier).")
    p_send.add_argument("task", help="The verbatim task for the recipient's agent.")
    p_send.add_argument("--expires", type=int, default=3600, help="TTL in seconds (60–86400). Default 3600.")
    p_send.add_argument("--cwd", help="Working-directory hint, stored in metadata.cwd.")
    p_send.add_argument("--meta", action="append", metavar="K=V", help="Extra metadata (repeatable).")

    add("sent", "List dispatches you've sent.", cmd_sent)
    add("inbox", "List dispatches addressed to you.", cmd_inbox)

    add("status", "Show one dispatch + its event trace.", cmd_status).add_argument(
        "dispatch_id", help="Full dispatch id (UUID).")
    add("accept", "Accept an inbound dispatch (daemon must be online).", cmd_accept).add_argument(
        "dispatch_id", help="Full dispatch id (UUID).")
    add("decline", "Decline an inbound dispatch.", cmd_decline).add_argument(
        "dispatch_id", help="Full dispatch id (UUID).")
    add("cancel", "Cancel a dispatch (either party).", cmd_cancel).add_argument(
        "dispatch_id", help="Full dispatch id (UUID).")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # SUPPRESS leaves these unset when not passed; normalize so every command
    # can read args.json / resolve broker+token uniformly.
    args.json = getattr(args, "json", False)
    config = _load_config()
    broker = _resolve_broker(getattr(args, "broker", None), config)
    token = _resolve_token(getattr(args, "token", None), config)

    if not token:
        sys.stderr.write(
            "error: no token. Sign in to the broker, run the daemon installer "
            "(which writes ~/.dispatch/config.json), or pass --token / set "
            "$DISPATCH_TOKEN.\n"
        )
        return 1

    try:
        return args.func(args, broker, token)
    except CliError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
