# Dispatch

Peer-to-peer agentic task courier. Any user can compose a task in natural
language, name a recipient, and the recipient's local daemon runs it on
their machine — with explicit consent on the dispatch itself and on every
destructive tool call.

The **broker** is a multi-tenant service (Postgres-backed FastAPI). Anyone
with an account can send to anyone else with an account. The **daemon** is
a small background process each user runs on their own machine to actually
receive and execute dispatches. The web UI is shared by both roles.

---

## Architecture

```
   browser     ┌──────────────────────────────────────────────────┐
   (sender +   │  Unified web UI   (served by the broker)         │
    recipient) │    /              login · compose · inbox · sent │
                └──────────────────────────────────────────────────┘
                                │ HTTPS / WSS
                                ▼
            ┌──────────────────────────────────────────────────┐
            │  BROKER  (FastAPI · dispatch.broker.app)         │
            │  HTTP                                            │
            │    POST /auth/request   → emails a magic link    │
            │    GET  /auth/magic     → exchanges for JWT      │
            │    POST /dispatch       sender creates dispatch  │
            │    GET  /dispatches     history list             │
            │    GET  /dispatch/{id}  full record              │
            │  WebSocket                                       │
            │    /agent/connect       recipient daemon         │
            │    /inbox               recipient browser        │
            │    /dispatch/{id}/watch sender browser           │
            │  Storage                                         │
            │    Postgres (users, dispatches, events, magic)   │
            └──────────────────────────────────────────────────┘
                                │ WSS
                                ▼
            ┌──────────────────────────────────────────────────┐
            │  RECIPIENT DAEMON  (`dispatch-daemon` CLI)       │
            │    - Pure WebSocket client to the broker         │
            │    - Awaits accept/reject and per-tool consent   │
            │      decisions forwarded from the broker UI      │
            │    - Runs the agent with the user's API key      │
            └──────────────────────────────────────────────────┘
                                │
                                ▼
                       Claude Agent SDK
                       (Read/Write/Edit/Bash/Glob/Grep)
```

Notes:
- The daemon has no UI of its own. All recipient interaction happens
  through the browser tab connected to the broker.
- `run_dispatch()` is transport-agnostic: the daemon imports and uses it
  exactly as a single-process server would.
- Per-tool consent flows: daemon emits `permission_request` over the
  broker WS → broker fans out to the recipient's `/inbox` WS → browser
  shows Allow/Deny → decision returns via `/inbox` → broker forwards as
  `tool_consent` to daemon → daemon's `can_use_tool` callback unblocks.

---

## Two ways to run the broker

The broker is a small FastAPI service. You can run it locally for development,
or deploy it to Railway for a stable public URL that your team can reach.

---

## A) Run the broker locally (development)

You need Postgres available. Easiest path is Docker:

```bash
docker run -d --name dispatch-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=dispatch \
  -p 5432:5432 postgres:16
```

Then:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env:
#   DISPATCH_JWT_SECRET=<any 32+ char string>
#   DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/dispatch

uvicorn --app-dir src dispatch.broker.app:app --env-file .env --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>.

For a teammate on another machine to reach you locally, use a tunnel:

```bash
ngrok http 8000             # or:  cloudflared tunnel --url http://localhost:8000
```

---

## B) Deploy the broker to Railway (recommended)

Production-ish setup with a stable URL and managed Postgres:

1. **Push the repo to GitHub.** Railway deploys from a repo.
2. **Create a Railway project.** <https://railway.app/new> → Deploy from GitHub
   repo → pick this repo. Railway detects Python, installs `requirements.txt`,
   and uses the `Procfile` (already in the repo) to start uvicorn.
3. **Add Postgres.** In the same project: New → Database → Add PostgreSQL.
   Railway injects `DATABASE_URL` automatically; the broker uses it on boot.
4. **Set environment variables** on the broker service:
   - `DISPATCH_JWT_SECRET` — a random 32+ char string (`openssl rand -hex 32`).
   - That's it. `DATABASE_URL` and `PORT` come from Railway automatically.
5. **Get the public URL.** Settings → Networking → Generate Domain. Railway
   gives you something like `https://dispatch-production-1234.up.railway.app`.
   Hand that URL to your teammate.

The broker exposes `/health` for Railway's health checks (configured in
`railway.json`). It returns 200 only when the database connection is alive.

### Cost

- Hobby tier ($5/mo, includes $5 of usage credit) is sufficient for a
  multi-person demo.
- Postgres add-on uses some of that credit; usually well under $5/mo at this
  scale.

### Local config files that drive the Railway deploy

| File | Purpose |
|---|---|
| `Procfile` | Tells the platform how to start the web service. |
| `railway.json` | Health check path, restart policy. |
| `runtime.txt` | Pins Python 3.11. |
| `.dockerignore` | Excludes `.venv`, `.env`, `workspace/`, etc. from builds. |

---

## Required environment

| Env var | Required on | Notes |
|---|---|---|
| `DISPATCH_JWT_SECRET` | Broker | 32+ random chars. Rotating it invalidates all issued tokens. |
| `DATABASE_URL` | Broker | Postgres URL. Auto-set by Railway Postgres add-on. |
| `ANTHROPIC_API_KEY` | Recipient daemon | The recipient pays for their own agent runs. |
| `DISPATCH_WORKSPACE` | Recipient daemon (optional) | Working directory for the agent. Default `./workspace`. |
| `DISPATCH_BROKER` / `DISPATCH_TOKEN` | Recipient daemon (optional) | Override CLI args. |

## Recipient daemon — installing it on a teammate's machine

The daemon is a single command after installation. They no longer need to
clone the repo, manage a venv, or remember the `python -m dispatch.daemon`
incantation.

### Option 1 — pipx from the GitHub repo (recommended)

```bash
brew install pipx                                                     # one-time
pipx install git+https://github.com/your-org/dispatch.git             # one-time

dispatch-daemon \\
    --broker https://your-broker-url \\
    --token <jwt-from-the-broker-login-page>
```

`pipx` creates an isolated venv per command, so the daemon's dependencies
don't pollute the system Python.

### Option 2 — pipx from a local clone

```bash
git clone https://github.com/your-org/dispatch.git
pipx install ./dispatch
dispatch-daemon --broker https://your-broker-url --token <jwt>
```

### Option 3 — plain venv (legacy)

The `python -m dispatch.daemon ...` invocation still works for anyone who'd
rather manage their own venv.

### How the recipient gets a token

Visit the broker's public URL in a browser, type their email, click **Send
link**. In dev mode (no email provider configured) the link is shown on the
page; in production they'll receive it by email. Clicking it signs them in,
and the page reveals the JWT they need for the daemon. Copy → paste into
`--token`.

### Recipient-side environment

Their `.env` (or shell) only needs `ANTHROPIC_API_KEY=…`. The daemon
defaults are fine for everything else.

---

### Sending a dispatch

You and the recipient both open the same broker URL in your browsers, both
sign in. Your daemon is running in the background (if you ever want to
receive). Theirs is running too. In the **Compose** form, type their email
as the recipient and a task description, hit **Send dispatch**.

What happens, in real time:

1. You see a new card under **Sent** with status `pending → delivered`.
2. Their **Inbox** lights up with the task. Two buttons: **Accept**, **Reject**.
3. They accept → status flips to `accepted → running`. The agent starts.
4. Each destructive tool call (`Write`, `Edit`, `Bash`) pauses the agent
   and shows them an **Allow / Deny** prompt inline. Allow it → tool runs.
5. The full event stream — reasoning, tool calls, tool results — appears
   live in **both** your **Sent** card and their **Inbox** card. You see
   what the agent is doing; you don't see their file system directly.

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
- **Recipient browser ↔ broker**, `wss://<broker>/inbox?token=<jwt>` — server streams inbox updates and per-dispatch events; client sends `dispatch_decision` and `tool_consent` frames.

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
  `Bash` require a click in the recipient's broker UI tab; auto-deny after 120s.
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
- **Recipient sees nothing in their inbox** — make sure their daemon is
  running and connected (it logs `connected. Open https://broker… in a
  browser…` when ready), and that they're signed into the broker URL in
  a browser as the same user.
- **Sender sees nothing after sending** — check the broker logs.
  `pending → delivered` requires the recipient daemon to be connected
  before the dispatch is created; otherwise the dispatch sits queued and
  flips to `delivered` when the daemon next connects.
