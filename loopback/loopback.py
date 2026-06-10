#!/usr/bin/env python3
"""Loopback test harness — be your own sender. No Edward required.

Creates a SECOND identity on this machine ("Fake Kaan") that acts as the
SENDER. You compose a dispatch as Fake Kaan from your terminal and send it to
your REAL account. Your real daemon receives it exactly like a dispatch from a
remote person would — so you can test the *receiving* side end to end, solo.

    Fake Kaan  ──dispatch──▶  your real account (kaaneroltu73@gmail.com)

Usage:
    python3 loopback.py up                       # create Fake Kaan + wire trust
    python3 loopback.py send "create note.txt saying hi"   # Fake Kaan -> you
    python3 loopback.py inbox                     # what your real account received
    python3 loopback.py accept <dispatch_id>      # accept it on your real account
    python3 loopback.py down                       # stop Fake Kaan (--purge to wipe)

Fake Kaan lives entirely under ~/.dispatch-fake on port 8011 with its own broker
identity. Nothing here disturbs your real ~/.dispatch daemon on port 8001.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# HTTPS to the broker needs a CA bundle; stock macOS Python urllib often can't
# find one. Prefer certifi (what the dispatch code itself uses).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# ----- configuration (override via env) --------------------------------------
REAL_HOME = Path(os.environ.get("DISPATCH_REAL_HOME", str(Path.home() / ".dispatch")))
REAL_PORT = int(os.environ.get("LOOPBACK_REAL_PORT", "8001"))

FAKE_USER = os.environ.get("LOOPBACK_FAKE_USER", "fake-kaan")
FAKE_HOME = Path(os.environ.get("LOOPBACK_FAKE_HOME", str(Path.home() / ".dispatch-fake")))
FAKE_PORT = int(os.environ.get("LOOPBACK_FAKE_PORT", "8011"))
FAKE_WORKSPACE = Path(os.environ.get("LOOPBACK_FAKE_WORKSPACE",
                                     str(Path.home() / "dispatch-fake" / "workspace")))

DAEMON_PID = FAKE_HOME / "loopback-daemon.pid"
DAEMON_LOG = FAKE_HOME / "daemon.log"


def _bin(name: str) -> str:
    cand = Path.home() / ".local" / "bin" / name
    if cand.exists():
        return str(cand)
    found = shutil.which(name)
    if not found:
        sys.exit(f"could not find '{name}' on PATH or in ~/.local/bin")
    return found


# ----- small helpers ---------------------------------------------------------
def _real_config() -> dict:
    try:
        return json.loads((REAL_HOME / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        sys.exit(f"cannot read your real config at {REAL_HOME/'config.json'}: {e}")


def _broker() -> str:
    c = _real_config()
    b = c.get("broker") or c.get("broker_url")
    if not b:
        sys.exit("no broker URL found in your real config")
    return b.rstrip("/")


def _real_user() -> str:
    """Your real identity = the 'sub' claim of your real broker token."""
    tok = _real_config().get("token", "")
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["sub"]
    except Exception:
        sys.exit("could not determine your real user id from the saved token")


def _local_token(home: Path) -> str:
    try:
        return (home / "local.token").read_text().strip()
    except OSError as e:
        sys.exit(f"cannot read local token at {home/'local.token'}: {e}")


def _http(method: str, url: str, *, token: str | None = None, body: dict | None = None,
          retries: int = 4, timeout: float = 15.0) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ctx = _SSL_CTX if url.startswith("https") else None
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            if e.code < 500 and e.code != 429:
                return e.code, payload
            last = (e.code, payload)
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            last = (0, str(e))
        time.sleep(0.6 * (attempt + 1))
    return last if last else (0, "request failed")


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ----- provisioning ----------------------------------------------------------
def mint_fake_token() -> str:
    status, payload = _http("POST", f"{_broker()}/auth/login", body={"username": FAKE_USER})
    if status != 200 or not isinstance(payload, dict) or "token" not in payload:
        sys.exit(f"failed to mint Fake Kaan token ({status}): {payload}")
    return payload["token"]


def provision_fake(token: str) -> None:
    FAKE_HOME.mkdir(parents=True, exist_ok=True)
    FAKE_WORKSPACE.mkdir(parents=True, exist_ok=True)
    real = _real_config()
    cfg = {
        "broker": _broker(),
        "username": FAKE_USER,
        "token": token,
        "anthropic_api_key": real.get("anthropic_api_key"),
        "workspace": str(FAKE_WORKSPACE),
        "local_port": FAKE_PORT,
    }
    path = FAKE_HOME / "config.json"
    path.write_text(json.dumps(cfg, indent=2))
    path.chmod(0o600)


def start_fake_daemon() -> None:
    """Fake Kaan's daemon must be ONLINE so the broker can ask it to sign the
    dispatches it sends. It does not need to receive anything."""
    if _alive(_read_pid(DAEMON_PID)):
        print(f"  Fake Kaan daemon already running (pid {_read_pid(DAEMON_PID)})")
        return
    cfg = json.loads((FAKE_HOME / "config.json").read_text())
    env = dict(os.environ)
    env["DISPATCH_HOME"] = str(FAKE_HOME)
    env["DISPATCH_LOCAL_PORT"] = str(FAKE_PORT)
    if cfg.get("anthropic_api_key"):
        env["ANTHROPIC_API_KEY"] = cfg["anthropic_api_key"]
    args = [
        _bin("dispatch-daemon"),
        "--broker", cfg["broker"],
        "--token", cfg["token"],
        "--workspace", cfg["workspace"],
        "--local-port", str(FAKE_PORT),
    ]
    if cfg.get("anthropic_api_key"):
        args += ["--anthropic-key", cfg["anthropic_api_key"]]
    logf = open(DAEMON_LOG, "ab")
    proc = subprocess.Popen(args, env=env, stdout=logf, stderr=logf, start_new_session=True)
    DAEMON_PID.write_text(str(proc.pid))
    print(f"  starting Fake Kaan daemon (pid {proc.pid}) on port {FAKE_PORT} …")

    deadline = time.time() + 40
    while time.time() < deadline:
        if not _alive(proc.pid):
            sys.exit(f"Fake Kaan daemon exited early — see {DAEMON_LOG}")
        if _port_open(FAKE_PORT) and (FAKE_HOME / "local.token").exists():
            st, _ = _http("GET", f"http://127.0.0.1:{FAKE_PORT}/api/invitations",
                          token=_local_token(FAKE_HOME), retries=1, timeout=8)
            if st == 200:
                print("  Fake Kaan online and connected to broker.")
                return
        time.sleep(1.0)
    sys.exit(f"Fake Kaan daemon did not come online — see {DAEMON_LOG}")


def _edge_fake_to_real_exists() -> bool:
    """True if Fake-Kaan -> real-you trust edge already exists (seen from your side)."""
    st, payload = _http("GET", f"http://127.0.0.1:{REAL_PORT}/api/trust",
                        token=_local_token(REAL_HOME))
    if st != 200 or not isinstance(payload, dict):
        return False
    for tl in payload.get("trust", []):
        if tl.get("direction") == "incoming" and tl.get("peer") == FAKE_USER:
            return True
    return False


def setup_edge(tools: list[str], approval: str, max_per_day: int) -> None:
    """Create the trust edge Fake-Kaan -> you, so Fake Kaan may dispatch to you.
    YOU are the trustor (recipient) and choose the scopes its agent runs under."""
    if _edge_fake_to_real_exists():
        print(f"  trust edge {FAKE_USER} -> you already exists.")
        return
    real_user = _real_user()
    fake_tok = _local_token(FAKE_HOME)
    real_tok = _local_token(REAL_HOME)

    # 1) Fake Kaan invites YOU (asks to be allowed to dispatch to you)
    st, payload = _http("POST", f"http://127.0.0.1:{FAKE_PORT}/api/invitations",
                        token=fake_tok, body={"to_email": real_user})
    if st not in (200, 201, 409):
        sys.exit(f"Fake Kaan invite failed ({st}): {payload}")

    # 2) YOUR account finds the pending invitation from Fake Kaan
    inv_token = None
    deadline = time.time() + 20
    while time.time() < deadline and not inv_token:
        st, payload = _http("GET", f"http://127.0.0.1:{REAL_PORT}/api/invitations",
                            token=real_tok)
        if st == 200 and isinstance(payload, dict):
            for r in payload.get("received", []):
                if r.get("from_user") == FAKE_USER and r.get("status", "pending") == "pending":
                    inv_token = r["token"]
                    break
        if not inv_token:
            time.sleep(1.0)
    if not inv_token:
        sys.exit("your account never saw Fake Kaan's invitation (broker lag/mismatch)")

    # 3) YOU accept, granting the scopes Fake Kaan's agent runs under
    scopes = {"tools": tools, "approval": approval, "max_dispatches_per_day": max_per_day}
    st, payload = _http("POST",
                        f"http://127.0.0.1:{REAL_PORT}/api/invitations/{inv_token}/accept",
                        token=real_tok, body={"scopes": scopes})
    if st != 200:
        sys.exit(f"accepting Fake Kaan's invite failed ({st}): {payload}")
    print(f"  trust edge created: {FAKE_USER} -> you   "
          f"tools={','.join(tools)} approval={approval}")


# ----- commands --------------------------------------------------------------
def cmd_up(a: argparse.Namespace) -> None:
    tools = [t.strip() for t in a.tools.split(",") if t.strip()]
    print(f"Creating Fake Kaan '{FAKE_USER}' (home={FAKE_HOME}, port={FAKE_PORT})")
    print(f"  -> will dispatch to your real account: {_real_user()}")
    token = mint_fake_token()
    provision_fake(token)
    start_fake_daemon()
    setup_edge(tools, a.approval, a.max_per_day)
    print("\nReady. Send a dispatch from Fake Kaan to yourself:\n"
          f"    python3 {Path(__file__).name} send \"create note.txt that says hello\"\n"
          f"Then on your real account:\n"
          f"    python3 {Path(__file__).name} inbox            # see what arrived\n"
          f"    python3 {Path(__file__).name} accept <id>      # accept it\n"
          f"  (or use your normal `dispatch inbox` / `dispatch accept` / the tray UI)")


def cmd_send(a: argparse.Namespace) -> None:
    """Fake Kaan composes a dispatch and sends it to your real account."""
    if not _alive(_read_pid(DAEMON_PID)):
        sys.exit("Fake Kaan daemon isn't running — run `up` first.")
    body = {
        "recipient_id": _real_user(),
        "task": a.task,
        "expires_in_seconds": a.expires,
        "metadata": ({"workflow": "true"} if a.workflow else {}),
    }
    print(f"Fake Kaan -> you: {a.task!r}")
    st, payload = _http("POST", f"http://127.0.0.1:{FAKE_PORT}/api/compose",
                        token=_local_token(FAKE_HOME), body=body, timeout=30)
    if st != 200 or not isinstance(payload, dict):
        sys.exit(f"send failed ({st}): {payload}")
    did = payload.get("dispatch_id")
    print(f"Sent. dispatch_id={did} status={payload.get('status','?')}")
    if not a.workflow:
        print("\nIt's now in your real inbox awaiting accept. See it with:\n"
              f"    python3 {Path(__file__).name} inbox\n"
              f"    python3 {Path(__file__).name} accept {did}")


def cmd_inbox(a: argparse.Namespace) -> None:
    """Show what your REAL account has received."""
    st, payload = _http("GET", f"http://127.0.0.1:{REAL_PORT}/api/inbox",
                        token=_local_token(REAL_HOME), retries=3)
    if st != 200 or not isinstance(payload, list):
        sys.exit(f"could not read your real inbox ({st}): {payload}")
    if not payload:
        print("(your real inbox is empty)")
        return
    print(f"your real inbox ({len(payload)} dispatch(es)):")
    for e in sorted(payload, key=lambda x: x.get("created_at", "")):
        print(f"  [{e.get('status'):<10}] {e.get('dispatch_id')}")
        print(f"      from {e.get('sender_id')}: {e.get('task','')[:70]!r}")


def cmd_accept(a: argparse.Namespace) -> None:
    """Accept a received dispatch on your REAL account (so it runs)."""
    st, payload = _http("POST",
                        f"http://127.0.0.1:{REAL_PORT}/api/dispatch/{a.dispatch_id}/decision",
                        token=_local_token(REAL_HOME), body={"decision": "accept"})
    if st == 200:
        print(f"accepted {a.dispatch_id} — your agent will run it now.")
    elif st == 409:
        print("no pending decision for that dispatch (already accepted, expired, "
              "or your daemon isn't actively holding it).")
    else:
        sys.exit(f"accept failed ({st}): {payload}")


def cmd_status(a: argparse.Namespace) -> None:
    print(f"Fake Kaan daemon : {'UP' if _port_open(FAKE_PORT) else 'DOWN'} "
          f"(port {FAKE_PORT}, pid {_read_pid(DAEMON_PID)})")
    print(f"your real daemon : {'UP' if _port_open(REAL_PORT) else 'DOWN'} (port {REAL_PORT})")
    print(f"edge {FAKE_USER}->you : {'present' if _edge_fake_to_real_exists() else 'absent'}")


def cmd_down(a: argparse.Namespace) -> None:
    pid = _read_pid(DAEMON_PID)
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(f"  stopped Fake Kaan daemon (pid {pid})")
    DAEMON_PID.unlink(missing_ok=True)
    if a.purge:
        shutil.rmtree(FAKE_HOME, ignore_errors=True)
        shutil.rmtree(FAKE_WORKSPACE.parent, ignore_errors=True)
        print(f"  purged {FAKE_HOME} and {FAKE_WORKSPACE.parent}")
    print("Fake Kaan is down.")


def main() -> None:
    p = argparse.ArgumentParser(description="Loopback dispatch harness — be your own sender.")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="create Fake Kaan + wire the trust edge to you")
    up.add_argument("--tools", default="Read,Glob,Grep,Write,Edit,Bash",
                    help="tools YOU grant Fake Kaan's agent (default: all)")
    up.add_argument("--approval", choices=["auto", "manual"], default="manual",
                    help="per-tool approval on your side (default: manual — you approve each call)")
    up.add_argument("--max-per-day", dest="max_per_day", type=int, default=500)
    up.set_defaults(func=cmd_up)

    snd = sub.add_parser("send", help="Fake Kaan dispatches a task to your real account")
    snd.add_argument("task", help="the task text")
    snd.add_argument("--expires", type=int, default=3600)
    snd.add_argument("--workflow", action="store_true",
                     help="send as workflow (auto-runs on your side without the accept step)")
    snd.set_defaults(func=cmd_send)

    sub.add_parser("inbox", help="show what your real account received").set_defaults(func=cmd_inbox)

    acc = sub.add_parser("accept", help="accept a received dispatch on your real account")
    acc.add_argument("dispatch_id")
    acc.set_defaults(func=cmd_accept)

    sub.add_parser("status", help="health of both daemons + the trust edge").set_defaults(func=cmd_status)

    dn = sub.add_parser("down", help="stop Fake Kaan")
    dn.add_argument("--purge", action="store_true", help="also delete Fake Kaan's home + workspace")
    dn.set_defaults(func=cmd_down)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
