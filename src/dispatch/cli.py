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
    dispatch invite <email>                 POST /invitations
    dispatch invitations                    GET  /invitations
    dispatch accept-invitation <token>      POST /invitations/{token}/accept
    dispatch decline-invitation <token>     POST /invitations/{token}/decline
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
import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

import certifi
import httpx


HTTP_TIMEOUT_S = 30.0


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


def _save_config(**fields: object) -> None:
    """Merge fields into ~/.dispatch/config.json (0600). Used by `login` to
    persist the broker URL + token the daemon/MCP read on the next run."""
    config = _load_config()
    config.update({k: v for k, v in fields.items() if v is not None})
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    try:
        path.chmod(0o600)  # bearer token lives here
    except OSError:
        pass


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
# Local daemon API (127.0.0.1) — the ONLY surface that resolves accept/reject
# and per-tool allow/deny.
#
# The daemon deliberately ignores decision/approval messages arriving over the
# broker WebSocket (so a compromised broker can't fabricate the human's
# intent); they must be delivered to the daemon's own loopback HTTP server,
# authenticated by the per-machine token at ~/.dispatch/local.token. That's
# why accept/decline/approve do NOT go through the broker.
# ----------------------------------------------------------------------------


def _local_port(config: dict) -> int:
    raw = os.environ.get("DISPATCH_LOCAL_PORT") or config.get("local_port") or 8001
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8001


def _local_token() -> str:
    path = _dispatch_home() / "local.token"
    try:
        tok = path.read_text().strip()
    except (FileNotFoundError, OSError):
        tok = ""
    if not tok:
        raise CliError(
            f"no local daemon token at {path}. The daemon writes it on start — "
            "is `dispatch-daemon` running on this machine?"
        )
    return tok


def _local_request(config: dict, method: str, path: str, **kw: Any) -> Any:
    """Call the local daemon's loopback HTTP API. Clean CliErrors on the
    failure modes a user will actually hit (daemon down, token stale, nothing
    pending)."""
    port = _local_port(config)
    base = f"http://127.0.0.1:{port}"
    token = _local_token()
    try:
        with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"},
                          timeout=HTTP_TIMEOUT_S) as c:
            resp = c.request(method, path, **kw)
    except httpx.ConnectError:
        raise CliError(
            f"the local daemon isn't reachable on {base}. Start `dispatch-daemon` "
            "on this machine and retry (accept/approve are resolved locally, not "
            "via the broker)."
        )
    except httpx.HTTPError as e:
        raise CliError(f"local daemon request to {path} failed: {e}")

    if resp.status_code == 401:
        raise CliError(
            f"local daemon rejected the token in {_dispatch_home()}/local.token. "
            "Restart `dispatch-daemon` to reissue it."
        )
    if resp.status_code == 404:
        raise CliError(
            "this daemon doesn't know that dispatch (404). It only resolves "
            "dispatches it received while running — check `dispatch inbox` for the "
            "full id, and confirm the daemon was up when it arrived."
        )
    if resp.status_code == 409:
        # No pending decision/approval for that id — already decided, expired,
        # or the daemon hasn't surfaced it yet.
        raise CliError(f"nothing pending: {_detail(resp)}")
    if resp.status_code >= 400:
        raise CliError(f"local daemon error {resp.status_code}: {_detail(resp)}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


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


def cmd_login(args: argparse.Namespace, broker: str, token: str) -> int:
    """Terminal-native sign-in via the device-authorization grant (RFC 8628).

    Starts a flow with the broker, opens the browser to approve (Clerk/Google
    sign-in), polls until approved, then saves broker + token to
    ~/.dispatch/config.json. Needs no existing token.
    """
    # Already signed in? Don't open a browser — verify the saved token first.
    if token and not getattr(args, "force", False):
        try:
            with httpx.Client(base_url=broker, timeout=HTTP_TIMEOUT_S, verify=certifi.where()) as c:
                me = c.get("/me", headers={"Authorization": f"Bearer {token}"})
            if me.status_code == 200:
                user = me.json().get("user_id", "(unknown)")
                _emit(
                    args,
                    {"status": "already_signed_in", "user_id": user, "broker": broker},
                    f"Already signed in as {user} on {broker}. "
                    f"Use `dispatch login --force` to re-authenticate.",
                )
                return 0
        except httpx.HTTPError:
            pass  # broker unreachable — fall through and try a fresh sign-in
    try:
        with httpx.Client(base_url=broker, timeout=HTTP_TIMEOUT_S, verify=certifi.where()) as c:
            resp = c.post("/auth/device")
    except httpx.HTTPError as e:
        raise CliError(f"can't reach broker at {broker} ({e}). Is the URL right?")
    if resp.status_code >= 400:
        raise CliError(f"broker error {resp.status_code}: {_detail(resp)}")
    start = resp.json()
    device_code = start["device_code"]
    user_code = start["user_code"]
    vuri = start.get("verification_uri_complete") or start.get("verification_uri")
    interval = max(1, int(start.get("interval", 5)))
    expires_in = int(start.get("expires_in", 600))

    # Instructions go to stderr so --json keeps stdout to the final result only.
    sys.stderr.write(
        f"\nSign in to Dispatch:\n"
        f"  1. Open:           {vuri}\n"
        f"  2. Confirm code:   {user_code}\n\n"
    )
    if not getattr(args, "no_browser", False):
        try:
            webbrowser.open(vuri)
        except Exception:
            pass
    sys.stderr.write("Waiting for browser approval… (Ctrl-C to cancel)\n")

    deadline = time.monotonic() + expires_in
    with httpx.Client(base_url=broker, timeout=HTTP_TIMEOUT_S, verify=certifi.where()) as c:
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                r = c.post("/auth/device/token", json={"device_code": device_code})
            except httpx.HTTPError:
                continue
            if r.status_code >= 400:
                continue
            body = r.json()
            st = body.get("status")
            if st == "approved":
                _save_config(broker=broker, token=body["token"])
                _emit(
                    args,
                    {"status": "ok", "user_id": body.get("user_id"), "broker": broker},
                    f"Signed in as {body.get('user_id')}. Saved to {_config_path()}.",
                )
                return 0
            if st == "expired":
                raise CliError("the code expired before you approved it. Run `dispatch login` again.")
            if st == "invalid":
                raise CliError("the broker rejected the device code. Run `dispatch login` again.")
            interval = max(interval, int(body.get("interval", interval)))
    raise CliError("timed out waiting for approval. Run `dispatch login` again.")


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


def cmd_invite(args: argparse.Namespace, broker: str, token: str) -> int:
    """Invite someone (by email) to let you dispatch to them. They must accept
    and set the scopes before any outgoing edge exists."""
    result = _request(broker, token, "POST", "/invitations", json={"to_email": args.email})
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result.get("delivered"):
        print(f"Invitation emailed to {result.get('to_email', args.email)}.")
    else:
        print(f"Invitation created for {result.get('to_email', args.email)} "
              "(email delivery is off on this broker).")
        if result.get("dev_link"):
            print(f"  Share this link:  {result['dev_link']}")
    print("They accept it (and choose the scopes your agent runs under) before "
          "you can `dispatch send` to them.")
    return 0


def cmd_invitations(args: argparse.Namespace, broker: str, token: str) -> int:
    """List pending invitations sent and received."""
    data = _request(broker, token, "GET", "/invitations")
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    sent = data.get("sent", [])
    received = data.get("received", [])
    if not sent and not received:
        print("No pending invitations.")
        return 0
    if received:
        print(f"Received ({len(received)}) — accept/decline with the token:")
        for inv in received:
            print(f"  from {inv['from_user']:<28} token={inv['token']}")
    if sent:
        print(f"Sent ({len(sent)}) — awaiting the invitee's acceptance:")
        for inv in sent:
            print(f"  to   {inv['to_email']:<28} [{inv.get('status', '?')}]")
    return 0


def cmd_accept_invitation(args: argparse.Namespace, broker: str, token: str) -> int:
    """Accept an invitation, setting the scopes the inviter's agent is confined
    to. The accepter (you) is the trustor — you control what's allowed."""
    scopes: dict[str, Any] = {
        "approval": args.approval,
        "max_dispatches_per_day": args.max_per_day,
    }
    if args.tools is not None:
        scopes["tools"] = [t.strip() for t in args.tools.split(",") if t.strip()]
    if args.paths is not None:
        scopes["paths"] = [p.strip() for p in args.paths.split(",") if p.strip()]
    result = _request(
        broker, token, "POST", f"/invitations/{args.token}/accept", json={"scopes": scopes}
    )
    _emit(
        args, result,
        f"Accepted. The inviter can now dispatch to you "
        f"(trust_link_id={result.get('trust_link_id', '?')}, approval={args.approval}).",
    )
    return 0


def cmd_decline_invitation(args: argparse.Namespace, broker: str, token: str) -> int:
    result = _request(broker, token, "POST", f"/invitations/{args.token}/decline")
    _emit(args, result, "Declined the invitation; no trust edge created.")
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
    if d.get("reply"):
        print(f"  reply:   {d['reply']}")
    events = d.get("events", [])
    if events:
        print(f"  events ({len(events)}):")
        for e in events:
            etype = e.get("type", "?")
            edata = e.get("data", e)
            print(f"    - {etype}: {json.dumps(edata) if not isinstance(edata, str) else edata}")
    return 0


DEFAULT_INSTALL_REPO = "git+https://github.com/kaan7305/dispatch.git"
_UPDATE_MARKER = Path.home() / ".dispatch" / "installed_commit"


def _install_spec(*, tray: bool) -> str:
    """The pip/pipx requirement to (re)install. With `tray`, carry the [tray]
    extra so the menu-bar app's pyobjc/rumps deps come along."""
    spec = os.environ.get("DISPATCH_INSTALL_SPEC", DEFAULT_INSTALL_REPO)
    if tray and "[" not in spec:
        # PEP 508 "name[extra] @ url" form — pipx/pip resolve the extra from the url.
        return f"dispatch-agent[tray] @ {spec}"
    return spec


def _tray_installed() -> bool:
    """Is the [tray] extra present in this (the installed) venv?"""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("objc", "rumps"))


def _git_url_of(spec: str) -> Optional[str]:
    """Pull the bare git URL out of a spec like 'name[tray] @ git+https://…git'."""
    s = spec.split(" @ ", 1)[1].strip() if " @ " in spec else spec
    if not s.startswith("git+"):
        return None
    return s[len("git+"):]


def _remote_head_sha(spec: str) -> Optional[str]:
    """SHA that `pipx install` would resolve `spec` to right now, via ls-remote.
    Honours an explicit '…git@branch' ref; otherwise asks for HEAD."""
    import subprocess
    url = _git_url_of(spec)
    if not url:
        return None
    ref = "HEAD"
    if "@" in url.split("://", 1)[-1]:
        url, ref = url.rsplit("@", 1)
    try:
        out = subprocess.run(["git", "ls-remote", url, ref],
                             capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.split()[0]


def _pipx_install(spec: str, *, capture: bool):
    """`pipx install --force <spec>` (falls back to `python -m pipx`)."""
    import shutil
    import subprocess
    pipx = shutil.which("pipx")
    cmd = [pipx, "install", "--force", spec] if pipx else \
        [sys.executable, "-m", "pipx", "install", "--force", spec]
    try:
        return subprocess.run(cmd, capture_output=capture, text=True)
    except FileNotFoundError:
        raise CliError("pipx not found. Install pipx, or reinstall dispatch manually:\n"
                       f"    pipx install --force '{spec}'")


# Files that ship in the Claude Code plugin bundle (served by the marketplace,
# NOT by pipx). A change to any of these is what makes `/plugin marketplace
# update` necessary; everything else is just code that pipx already refreshed.
_PLUGIN_PATH_PREFIXES = (".claude-plugin/", "skills/")


def _plugin_files_changed(spec: str, base_sha: str, head_sha: Optional[str]) -> Optional[bool]:
    """Did any plugin-bundled file change between `base_sha` and `head_sha`?
    Uses GitHub's compare API (public repo, no auth). Returns True/False, or
    None when it can't be determined (no prior marker, private repo, network
    error) — the caller then shows a hedged reminder."""
    import re
    url = _git_url_of(spec)
    if not url or not base_sha or not head_sha or base_sha == head_sha:
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    api = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    try:
        with httpx.Client(timeout=15.0, verify=certifi.where()) as c:
            r = c.get(api, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        files = r.json().get("files") or []
    except ValueError:
        return None
    return any(
        str(f.get("filename", "")).startswith(_PLUGIN_PATH_PREFIXES) for f in files
    )


def cmd_update(args: argparse.Namespace, broker: str, token: str) -> int:
    """Self-update: reinstall the dispatch package (CLI + daemon + MCP server)
    from the latest source via pipx, so your local commands match `main`.
    No-ops when already at the latest commit unless --force is given."""
    want_tray = bool(getattr(args, "tray", False)) or _tray_installed()
    spec = _install_spec(tray=want_tray)
    remote = _remote_head_sha(spec)
    try:
        local = _UPDATE_MARKER.read_text().strip()
    except OSError:
        local = ""

    if not args.force and remote is not None and local and local == remote:
        _emit(
            args,
            {"status": "current", "commit": remote, "spec": spec},
            f"Already up to date ({remote[:10]}). Use `dispatch update --force` "
            "to reinstall anyway.",
        )
        return 0

    sys.stderr.write(f"dispatch: updating from {spec} …\n")
    proc = _pipx_install(spec, capture=not args.json)
    if proc.returncode != 0:
        raise CliError(f"update failed (pipx exit {proc.returncode}). "
                       + ((proc.stderr or "").strip()[-400:] if not args.json else ""))

    # Did the plugin bundle (skill text / manifest) change? Only then does the
    # user need `/plugin marketplace update` — pipx already refreshed the code.
    plugin_changed = _plugin_files_changed(spec, local, remote)

    if remote:
        try:
            _UPDATE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _UPDATE_MARKER.write_text(remote)
        except OSError:
            pass

    message = (
        "Updated. Restart your Claude Code session so the in-session dispatch-mcp "
        "reloads the new code (a running process keeps the old code until it restarts)."
    )
    if plugin_changed is True:
        message += (" The plugin's skill/manifest changed — also run `/plugin "
                    "marketplace update dispatch` in Claude Code.")
    elif plugin_changed is None:
        message += (" If the skill text or manifest changed, also run `/plugin "
                    "marketplace update dispatch` in Claude Code.")
    _emit(
        args,
        {"status": "updated", "spec": spec, "commit": remote,
         "plugin_changed": plugin_changed},
        message,
    )
    return 0


def cmd_tray(args: argparse.Namespace, broker: str, token: str) -> int:
    """Launch the macOS menu-bar app (always-on daemon supervisor). Replaces
    this process with `dispatch-tray`. Self-heals the [tray] extra (pyobjc/rumps)
    if it's missing — the bare install doesn't include those."""
    import shutil
    exe = shutil.which("dispatch-tray")
    if not exe or not _tray_installed():
        sys.stderr.write(
            "dispatch: tray dependencies (pyobjc/rumps) missing — installing the "
            "[tray] extra …\n"
        )
        proc = _pipx_install(_install_spec(tray=True), capture=not args.json)
        if proc.returncode != 0:
            raise CliError(
                f"could not install the tray extra (pipx exit {proc.returncode}). "
                "Install it manually:\n"
                f"    pipx install --force '{_install_spec(tray=True)}'"
            )
        exe = shutil.which("dispatch-tray") or exe
        if not exe:
            raise CliError("dispatch-tray still not found after installing the extra.")
    os.execv(exe, [exe])  # replace the CLI process with the tray app


def cmd_cancel(args: argparse.Namespace, broker: str, token: str) -> int:
    result = _request(broker, token, "POST", f"/dispatch/{args.dispatch_id}/cancel")
    status = result.get("status", "?")
    if status == "noop":
        _emit(args, result, f"Already terminal ({result.get('current_status')}); nothing to cancel.")
    else:
        _emit(args, result, f"Cancelled {args.dispatch_id}.")
    return 0


# ----------------------------------------------------------------------------
# accept / decline / tool-approvals — resolved by the LOCAL daemon, not the
# broker. (See _local_request above for why.)
# ----------------------------------------------------------------------------


def _decide_local(args: argparse.Namespace, config: dict, *, decision: str) -> int:
    target = args.dispatch_id
    _local_request(
        config, "POST", f"/api/dispatch/{target}/decision", json={"decision": decision}
    )
    verb = "Accepted" if decision == "accept" else "Declined"
    payload = {"dispatch_id": target, "decision": decision, "ok": True}
    tail = " The agent is starting; tool calls still need per-call approval " \
           "under a `manual` edge — watch `dispatch approvals`." if decision == "accept" else ""
    _emit(args, payload, f"{verb} {_short(target)}.{tail}")
    return 0


def cmd_accept(args: argparse.Namespace, broker: str, token: str) -> int:
    return _decide_local(args, _load_config(), decision="accept")


def cmd_decline(args: argparse.Namespace, broker: str, token: str) -> int:
    return _decide_local(args, _load_config(), decision="reject")


def cmd_approvals(args: argparse.Namespace, broker: str, token: str) -> int:
    """List tool calls currently waiting for allow/deny on this daemon."""
    config = _load_config()
    entries = _local_request(config, "GET", "/api/inbox")
    pending = []
    for e in entries if isinstance(entries, list) else []:
        for request_id, info in (e.get("pending_tools") or {}).items():
            pending.append({
                "dispatch_id": e["dispatch_id"],
                "request_id": request_id,
                "tool": info.get("tool"),
                "input": info.get("input"),
                "sender_id": e.get("sender_id"),
            })
    if args.json:
        print(json.dumps(pending, indent=2))
        return 0
    if not pending:
        print("No tool calls awaiting approval.")
        return 0
    print(f"Pending tool approvals ({len(pending)}):")
    for p in pending:
        inp = json.dumps(p["input"]) if not isinstance(p["input"], str) else p["input"]
        if len(inp) > 80:
            inp = inp[:77] + "…"
        print(f"  {_short(p['dispatch_id'])}  req={p['request_id']}  {p['tool']}  {inp}")
        print(f"      approve: dispatch approve {p['dispatch_id']} {p['request_id']}")
    return 0


def _tool_decide(args: argparse.Namespace, *, decision: str) -> int:
    config = _load_config()
    _local_request(
        config, "POST",
        f"/api/dispatch/{args.dispatch_id}/tool/{args.request_id}/decision",
        json={"decision": decision},
    )
    verb = "Allowed" if decision == "allow" else "Denied"
    payload = {"dispatch_id": args.dispatch_id, "request_id": args.request_id,
               "decision": decision, "ok": True}
    _emit(args, payload, f"{verb} tool call {args.request_id} on {_short(args.dispatch_id)}.")
    return 0


def cmd_approve(args: argparse.Namespace, broker: str, token: str) -> int:
    return _tool_decide(args, decision="allow")


def cmd_deny(args: argparse.Namespace, broker: str, token: str) -> int:
    return _tool_decide(args, decision="deny")


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
    # No subcommand → print help instead of erroring.
    sub = parser.add_subparsers(dest="command", required=False)

    def add(name: str, help: str, func) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, parents=[common])
        p.set_defaults(func=func)
        return p

    p_login = add("login", "Sign in from the terminal (device-authorization flow).", cmd_login)
    p_login.add_argument("--no-browser", action="store_true", default=False,
                         help="Don't auto-open the browser; just print the URL + code.")
    p_login.add_argument("--force", action="store_true", default=False,
                         help="Re-authenticate even if already signed in.")
    p_login.set_defaults(no_auth=True)  # login is how you GET a token

    # Launch the menu-bar app (no broker creds needed).
    add("tray", "Launch the macOS menu-bar app (always-on daemon supervisor).",
        cmd_tray).set_defaults(no_auth=True)

    # Self-update the installed package (no broker creds needed).
    p_update = add("update",
                   "Update the dispatch CLI/daemon/MCP from the latest source (pipx).",
                   cmd_update)
    p_update.set_defaults(no_auth=True)
    p_update.add_argument("--force", action="store_true", default=False,
                          help="Reinstall even if already at the latest commit.")
    p_update.add_argument("--tray", action="store_true", default=False,
                          help="Also (re)install the macOS [tray] extra (pyobjc/rumps).")

    # `dispatch help` → print top-level usage.
    def _cmd_help(_args: argparse.Namespace, _broker: str, _token: str) -> int:
        parser.print_help()
        return 0
    add("help", "Show this help.", _cmd_help).set_defaults(no_auth=True)

    add("whoami", "Show the signed-in user + broker.", cmd_whoami)
    add("contacts", "List trust edges (who can dispatch to whom).", cmd_contacts)

    # Invitations & trust establishment (broker-backed).
    add("invite", "Invite someone (by email) to let you dispatch to them.",
        cmd_invite).add_argument("email", help="Invitee's email address.")
    add("invitations", "List pending invitations you've sent and received.", cmd_invitations)

    p_acc_inv = add("accept-invitation",
                    "Accept an invitation, setting the scopes the inviter's agent runs under.",
                    cmd_accept_invitation)
    p_acc_inv.add_argument("token", help="Invitation token (from `dispatch invitations`).")
    p_acc_inv.add_argument(
        "--tools", default=None,
        help="Comma-separated allowed tools ⊆ Read,Glob,Grep,Write,Edit,Bash. "
             "Default: Read,Glob,Grep (read-only).")
    p_acc_inv.add_argument(
        "--paths", default=None,
        help="Comma-separated directory allowlist. Default: no path restriction.")
    p_acc_inv.add_argument(
        "--approval", choices=["manual", "auto"], default="manual",
        help="'manual' (approve every tool call, default) or 'auto'.")
    p_acc_inv.add_argument(
        "--max-per-day", dest="max_per_day", type=int, default=50,
        help="Max dispatches/day on this edge (1–10000). Default 50.")

    add("decline-invitation", "Decline an invitation (no trust edge created).",
        cmd_decline_invitation).add_argument(
        "token", help="Invitation token (from `dispatch invitations`).")

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
    add("cancel", "Cancel a dispatch (either party).", cmd_cancel).add_argument(
        "dispatch_id", help="Full dispatch id (UUID).")

    # Local-only commands: resolved by THIS machine's daemon (127.0.0.1), not
    # the broker. They don't need broker creds.
    def add_local(name: str, help: str, func) -> argparse.ArgumentParser:
        p = add(name, help, func)
        p.set_defaults(local_only=True)
        return p

    add_local("accept", "Accept an inbound dispatch (resolved by your local daemon).",
              cmd_accept).add_argument("dispatch_id", help="Full dispatch id (UUID).")
    add_local("decline", "Decline an inbound dispatch (local daemon).",
              cmd_decline).add_argument("dispatch_id", help="Full dispatch id (UUID).")
    add_local("approvals", "List tool calls awaiting allow/deny on your local daemon.",
              cmd_approvals)
    p_approve = add_local("approve", "Allow a pending tool call (manual-approval edge).", cmd_approve)
    p_approve.add_argument("dispatch_id", help="Full dispatch id (UUID).")
    p_approve.add_argument("request_id", help="Tool-call request id (from `dispatch approvals`).")
    p_deny = add_local("deny", "Deny a pending tool call.", cmd_deny)
    p_deny.add_argument("dispatch_id", help="Full dispatch id (UUID).")
    p_deny.add_argument("request_id", help="Tool-call request id (from `dispatch approvals`).")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Bare `dispatch` (no subcommand) → show help instead of an argparse error.
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0

    # SUPPRESS leaves these unset when not passed; normalize so every command
    # can read args.json / resolve broker+token uniformly.
    args.json = getattr(args, "json", False)
    config = _load_config()
    broker = _resolve_broker(getattr(args, "broker", None), config)
    token = _resolve_token(getattr(args, "token", None), config)

    # Local-only commands (accept/decline/approve/deny/approvals) talk to the
    # loopback daemon and don't need broker creds.
    local_only = getattr(args, "local_only", False)
    no_auth = getattr(args, "no_auth", False)  # `login` — it's how you get a token
    if not token and not local_only and not no_auth:
        sys.stderr.write(
            "error: no token. Run `dispatch login` to sign in from the terminal, "
            "or pass --token / set $DISPATCH_TOKEN.\n"
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
