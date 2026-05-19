# Dispatch

Peer-to-peer agentic task courier. One user composes a task in natural
language, names a recipient, and the recipient's local daemon runs it on
their machine — with explicit consent on the dispatch itself and on every
destructive tool call.

This repo runs end-to-end across machines today. You operate the **broker**,
your teammate runs the **recipient daemon**, and dispatches flow between
you.

---

## Architecture (current state)

```
            ┌──────────────────────────────────────────────────┐
   browser  │  Sender web UI   (served by the broker)          │
   tab A   ─┤    /              login → compose → watch        │
            └──────────────────────────────────────────────────┘
                                │ HTTPS / WSS
                                ▼
            ┌──────────────────────────────────────────────────┐
            │  BROKER  (FastAPI · dispatch.broker.app)         │
            │    POST /auth/login          → JWT bearer        │
            │    POST /dispatch            (sender → broker)   │
            │    WS   /agent/connect       (recipient daemon)  │
            │    WS   /dispatch/{id}/watch (sender watches)    │
            └──────────────────────────────────────────────────┘
                                │ WSS
                                ▼
            ┌──────────────────────────────────────────────────┐
            │  RECIPIENT DAEMON  (python -m dispatch.daemon)   │
            │    - WS client to broker                         │
            │    - Local consent UI on http://127.0.0.1:8001   │
            │    - Runs run_dispatch() with the user's API key │
            └──────────────────────────────────────────────────┘
                                │
                  ┌─────────────┴────────────┐
                  ▼                          ▼
            browser tab B           Claude Agent SDK
            (consent UI)            (Read/Write/Edit/Bash/...)
```

The executor (`run_dispatch`) is transport-agnostic — the daemon and the
broker reuse the same generator unchanged.

---

## Setup (both machines)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### On the broker machine (you)

Edit `.env`:
- `DISPATCH_JWT_SECRET` — any random 32+ char string. **Same secret must be
  set whenever the broker is restarted, or all issued tokens become invalid.**

You do **not** need `ANTHROPIC_API_KEY` on this machine — the broker never
runs the agent.

### On the recipient machine (your teammate)

Edit `.env`:
- `ANTHROPIC_API_KEY` — the recipient's own key. The agent runs locally and
  charges against this key.

They do **not** need `DISPATCH_JWT_SECRET` — only the broker verifies tokens.

---

## Run it

### 1. Start the broker

```bash
.venv/bin/uvicorn --app-dir src dispatch.broker.app:app \
    --env-file .env --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. Log in as yourself (any username) — you'll get
back a JWT token. The page will show your token so you can copy it.

### 2. Expose the broker (if recipient is on a different machine)

For a quick demo, use a tunnel. `ngrok` is easiest:

```bash
ngrok http 8000
```

Note the `https://....ngrok-free.app` URL — that's what your teammate uses
as `--broker`.

For production: deploy the broker behind a TLS-terminating reverse proxy,
bind to a public hostname.

### 3. Your teammate clones the repo, sets up their venv, then:

a. **Gets a token.** Visit the broker URL in a browser, sign in with their
   chosen username, and copy the JWT shown on the page. (Equivalent CLI:
   `curl -X POST https://broker/auth/login -H 'Content-Type: application/json' -d '{"username":"teammate"}'`)

b. **Runs the daemon:**

   ```bash
   python -m dispatch.daemon \
       --broker https://broker-url \
       --token <jwt-from-step-a> \
       --ui-port 8001
   ```

   The daemon connects to the broker and opens
   <http://127.0.0.1:8001> in their browser. This is their consent UI.

### 4. You send them a dispatch

Back in your sender UI, type their username as the recipient and a task,
then hit **Send dispatch**. You'll see the dispatch card appear with live
status. Your teammate's consent UI will show the incoming task — they press
**Accept**, the agent starts, and destructive tools prompt them
individually. Every event streams back to your watch view.

---

## What the teammate sees

```
Inbox
├── From <you> · pending
│   Task: "Bootstrap the test fixtures in this repo"
│   [Accept]  [Reject]
│
└── (once accepted, events stream here)
        agent       Looking at the test directory…
        tool call   Bash { command: "ls tests/" }
        tool result  fixture_a.json  fixture_b.json
        consent     Agent wants to run Write { file_path: "..." }   [Allow] [Deny]
        ...
        done        4 turns · $0.04
```

---

## What you see (sender)

```
Active dispatches
└── → teammate · running
    Task: "Bootstrap the test fixtures in this repo"
        status              accepted → running
        agent               Looking at the test directory…
        tool call           Bash { command: "ls tests/" }
        tool result         fixture_a.json  fixture_b.json
        consent requested   Awaiting recipient's decision on Write
        consent decided     Write: allow
        ...
        done                4 turns · $0.04
```

You never see the recipient's local file system; you see the agent's
reasoning, its tool calls, the recipient's consent decisions, and the
results.

---

## Wire protocols

### Sender HTTP

| Verb | Path                  | Auth   | Body / Returns |
| ---- | --------------------- | ------ | -------------- |
| POST | `/auth/login`         | none   | `{username}` → `{user_id, token}` |
| POST | `/dispatch`           | Bearer | `{recipient_id, task, expires_in_seconds?, metadata?}` → `{dispatch_id, status}` |
| GET  | `/dispatch/{id}`      | Bearer | full record |

### WebSockets

- **Sender → broker**, `wss://<broker>/dispatch/{id}/watch?token=<jwt>` — broker streams DispatchEvents.
- **Recipient daemon → broker**, `wss://<broker>/agent/connect?token=<jwt>`
  - Broker → daemon: `{type:"new_dispatch", payload:{...DispatchPayload...}}`
  - Daemon → broker: `{type:"dispatch_status", dispatch_id, status}` and `{type:"dispatch_event", dispatch_id, event:{...DispatchEvent...}}`
- **Recipient daemon ↔ local UI**, `ws://127.0.0.1:<ui-port>/ws/inbox` and `/ws/dispatch/{id}`.

### DispatchEvent shape (uniform across all layers)

```json
{ "type": "agent_text",         "data": { "text": "..." } }
{ "type": "tool_use",           "data": { "name": "Bash", "input": {...} } }
{ "type": "tool_result",        "data": { "content": "...", "is_error": false } }
{ "type": "permission_request", "data": { "id": "...", "tool": "Write", "input": {...} } }
{ "type": "permission_response","data": { "tool": "Write", "decision": "allow" } }
{ "type": "dispatch_status",    "data": { "status": "running" } }
{ "type": "done",               "data": { "subtype": "success", "duration_ms": 4200, ... } }
{ "type": "error",              "data": { "exception": "...", "message": "..." } }
```

### DispatchPayload (signed-over envelope)

```json
{
  "dispatch_id": "8e3a...",
  "sender_id": "you",
  "recipient_id": "teammate",
  "task": "...",
  "created_at": "2026-05-19T18:00:00Z",
  "expires_at": "2026-05-19T19:00:00Z",
  "metadata": {}
}
```

---

## Safety model (current)

- The recipient's agent runs with `permission_mode="default"`. Read-only
  tools (`Read`, `Glob`, `Grep`) are auto-approved. `Write`, `Edit`, and
  `Bash` require a click in the recipient's local UI; auto-deny after 120s.
- The recipient must `Accept` the whole dispatch before any agent runs.
  Auto-expire after 5 minutes if not answered.
- The agent runs with `cwd=./workspace` by default. `Bash` itself is **not**
  sandboxed to that directory — clicking Allow on a shell command means
  trusting it the way you'd trust typing it yourself.
- Tokens are JWTs signed with `DISPATCH_JWT_SECRET`. Anyone with the secret
  can forge tokens; keep it on the broker only.

---

## Project layout

```
src/dispatch/
  shared/
    schema.py        DispatchPayload, DispatchEvent, DispatchStatus, LoginRequest
    identity.py      JWT issue/verify (HS256, configurable secret)
  executor/
    executor.py      run_dispatch() — transport-agnostic, reused by daemon
  broker/
    state.py         in-memory users / dispatches / live agent connections
    app.py           FastAPI broker + sender static mount
  daemon/
    main.py          CLI entry point + WS client to broker
    local_app.py     local FastAPI for the recipient's consent UI
  web/
    sender/          login + compose + watch (served by broker)
    recipient/       inbox + consent UI (served by daemon)
```

---

## Limitations on purpose (next things)

- In-memory state only; restart loses everything.
- Login takes any username — no real authentication. Swap `/auth/login` for
  a GitHub/Google OAuth callback to harden.
- One recipient per dispatch; no fan-out.
- No signed payloads (tokens cover identity for the demo).
- No persistent dispatch history; sender's "Active dispatches" view doesn't
  survive a page refresh.

---

## Troubleshooting

- **`ModuleNotFoundError: dispatch`** — run with `--app-dir src` (broker) or
  `python -m dispatch.daemon` from the repo root (daemon).
- **Daemon: `handshake failed: 1008`** — your token is invalid or expired,
  or the broker is running with a different `DISPATCH_JWT_SECRET` than the
  one that issued the token.
- **Daemon: `could not reach broker`** — `--broker` URL is wrong, the
  broker isn't running, or your firewall/tunnel is blocking it.
- **No consent UI opens on the recipient** — pass `--no-open` and visit
  `http://127.0.0.1:8001` (or whatever `--ui-port` you set) yourself.
- **Sender sees nothing after sending** — check the broker logs.
  `pending → delivered` requires the recipient daemon to be connected
  before the dispatch is created; otherwise the dispatch sits queued and
  flips to `delivered` when the daemon next connects.
