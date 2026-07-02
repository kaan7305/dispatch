# Dispatch

A peer-to-peer courier for **agentic work**. One person describes a task in
natural language and names a recipient; the recipient's machine runs it with
a local AI agent - but only if the two people have an explicit, scoped,
revocable **trust relationship**, the dispatch is **cryptographically signed**
by the sender's device, and the recipient **approves** it.

A dispatch never runs on a machine unless all three hold:

1. an accepted **trust edge** exists from sender to recipient,
2. the dispatch carries a valid **signature** from a device of that sender,
3. it stays within the **scope** that edge grants - and destructive tool
   calls get a per-call human **approval**.

---

## Components

| Component | What it is |
|---|---|
| **Broker** | Multi-tenant FastAPI service, Postgres-backed. Issues identity, routes dispatches, enforces trust policy, relays events. Never holds a signing key; never touches a recipient's filesystem. |
| **Daemon** (`dispatch-daemon`) | A small background process each user runs on their own machine. Holds that machine's Ed25519 device key, signs the user's outgoing dispatches, verifies incoming ones, and runs the agent. |
| **Web UI** | A React app (`web/desktop`) served both by the broker (`/app`) and by each daemon on `127.0.0.1`: sign in, manage contacts + per-edge permissions, compose, watch sent dispatches, act on your inbox, run workflows. |
| **MCP server** (`dispatch-mcp`) | A thin in-session client Claude Code launches per session. Holds no broker connection, key, or executor - it ensures a daemon is running and drives it over the daemon's loopback API so Dispatch works from inside Claude. |
| **Agent** | A Claude Agent SDK session the daemon opens per accepted dispatch, on a clean base (`setting_sources=[]`). Transient - created on accept, gone when the task ends. |

Every user is both a sender and a recipient; everyone runs a daemon.

---

## The three enforcement layers

Trust is enforced in three independent places. A compromised broker cannot
forge a trusted sender, because Layer 2 happens entirely on the recipient's
machine against a key the broker can't substitute.

```
   browser ── HTTPS/WSS ──▶  BROKER  ── WSS ──▶  RECIPIENT DAEMON ──▶ Claude Agent SDK
                              │                       │
   Layer 1 ───────────────────┘                       │
   "Is there an accepted trust edge? In scope?         │
    Under the rate limit?"  → else 403/429             │
                                                       │
   Layer 2 ────────────────────────────────────────────┘
   "Is the signature valid? Key pinned? Nonce fresh & unseen?"  → else reject
                                                       │
   Layer 3 ────────────────────────────────────────────┘
   "Does the human approve this specific tool call?"  → manual / auto per scope
```

---

## Concepts

- **User** - identified by email. Created on first sign-in.
- **Device** - one per machine running the daemon. Has an Ed25519 keypair;
  the private key never leaves the machine (OS keychain, or a 0600 file).
- **Invitation** - an emailed, single-use, expiring link. How a trust edge
  is born.
- **Trust edge** - a directed `from_user → to_user` relationship. Created
  when the recipient accepts an invitation. Per-direction: B accepting A's
  invite lets *A dispatch to B*, nothing more.
- **Scopes** - what an edge permits, set by the recipient (the trustor):
  ```json
  {
    "tools": ["Read", "Glob", "Grep"],     // subset of Read/Write/Edit/Bash/Glob/Grep
    "mcp": [],                             // MCP servers a dispatch may use: bare
                                           //   names (["gog"]) or ["*"] for all
    "paths": ["~/work"],                   // file-path allowlist ([] = no path limit)
    "approval": "manual",                  // "manual" = approve every tool call | "auto"
    "max_dispatches_per_day": 50,
    "expires_at": null
  }
  ```
  New edges default to least privilege: read-only tools, no MCP, manual approval.
- **Dispatch** - one task. Signed by the sender's device, authorized by an
  edge, executed by the recipient's daemon.

### Letting dispatches use your MCP tools

A dispatch can use the recipient's **own MCP servers** (their Notion, search,
domain tools) - that's what makes it powerful - without exposing the rest of
your machine. **No manual setup:**

1. **Auto-discovery.** Your installed MCP servers are discovered automatically
   from your Claude config (`~/.claude.json` - user, per-project, and each
   project's `.mcp.json`). Nothing to curate. (An optional
   `~/.dispatch/shareable-mcp.json`, same shape as a Claude `.mcp.json`, can add
   or override a server the scan can't see; it wins on a name clash.) The
   `dispatch` control plane is never exposable.
2. **Pick at invite time (per sender).** When someone accepts your invite - or
   when you edit an edge - you choose which of your servers that sender may use:
   "Allow all", or a per-server Allow/Don't pick. The choice lands in the edge's
   `mcp` scope (bare server names, or `*` for all). Edit or revoke any time with
   `dispatch set-scope` / `dispatch revoke`, or in the web UI's **Edit
   permissions** dialog.

A dispatch is handed **only** the servers its edge scoped - unscoped servers are
never launched, attempted, or even visible to the task. And the task runs on a
**clean base** (it does *not* inherit your plugins, skills, or other MCP
config), so the edge's `mcp` grant is the entire reachable surface, gated
per-call by `can_use_tool`.

---

## Running the broker

The broker needs Postgres. Run it locally for development or deploy it.

### A) Locally

```bash
docker run -d --name dispatch-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=dispatch -p 5432:5432 postgres:16

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set DISPATCH_JWT_SECRET; DATABASE_URL points at the docker pg

uvicorn --app-dir src dispatch.broker.app:app --env-file .env --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. For a teammate on another machine:
`cloudflared tunnel --url http://localhost:8000` (or `ngrok http 8000`).

### B) Railway (managed)

1. Push the repo to GitHub; create a Railway project from it (Nixpacks
   auto-detects Python and uses the `Procfile`).
2. Add the **PostgreSQL** plugin. Wire it: on the broker service, add a
   variable `DATABASE_URL = ${{Postgres.DATABASE_URL}}`.
3. Set `DISPATCH_JWT_SECRET` (`openssl rand -hex 32`). `RAILWAY_PUBLIC_DOMAIN`
   is auto-injected - the broker uses it as its public URL, so invitation links
   and the install one-liner are correct without extra config.
4. Settings → Networking → Generate Domain. `/health` is the healthcheck.

The broker schema is idempotent (`broker/schema.sql`, `CREATE TABLE IF NOT
EXISTS` + `ADD COLUMN IF NOT EXISTS`) and runs on every startup - no
migration step.

### Environment

| Var | Where | Notes |
|---|---|---|
| `DISPATCH_JWT_SECRET` | Broker | 32+ random chars. Rotating it invalidates all tokens. |
| `DATABASE_URL` | Broker | Postgres URL. Auto-set by Railway's plugin. |
| `DISPATCH_PUBLIC_URL` | Broker (optional) | Public base URL; falls back to `RAILWAY_PUBLIC_DOMAIN`, then localhost. |
| `CLERK_*` | Broker (optional) | `CLERK_PUBLISHABLE_KEY`, `CLERK_FRONTEND_API`, `CLERK_JWT_AUDIENCE`, `CLERK_JWT_TEMPLATE` - enable Clerk (Google) sign-in for the web UI. Without them, use dev `/auth/login` (set `DISPATCH_DEV_AUTH=1`). |
| `DISPATCH_DEV_AUTH` | Broker (optional) | Set to `1` to enable the passwordless `POST /auth/login` for local dev. It mints a JWT for any username, so it is off by default and **always refused when Clerk is configured** - never enable it on a real deployment. |
| `RESEND_API_KEY`, `RESEND_FROM` | Broker (optional) | Real email for invitation links. Without it, links are returned in the API response (dev mode). |
| `DISPATCH_DAEMON_INSTALL` | Broker (optional) | What `/install.sh` points `pipx install` at. Default: a GitHub repo URL. |
| `ANTHROPIC_API_KEY` | Daemon | The recipient runs the agent on their own key. |
| `DISPATCH_WORKSPACE` | Daemon (optional) | Agent working directory. Default `./workspace`. |
| `DISPATCH_HOME` | Daemon (optional) | Where config + device key live. Default `~/.dispatch`. |
| `DISPATCH_KEY_BACKEND` | Daemon (optional) | `file` to store the device key in a 0600 file instead of the OS keychain. |

---

## Two ways to set up the local agent

The trust layers need a local **daemon** that holds your device key,
signs/verifies, runs the agent, and holds the approval prompts. There's always
exactly **one daemon per machine** (it owns the single broker connection, guarded
by an advisory lock in `~/.dispatch/connection.lock`). The two setups differ only
in *how the daemon gets started*: have your **Claude Code session auto-spawn it**,
or run the **installer** so it's always on.

### A) Via Claude Code (auto-spawn) - the low-friction default

Install the Dispatch **plugin** for Claude Code. It bundles the `/dispatch` skill
*and* an MCP server (`dispatch-mcp`) that Claude Code launches each session. The
MCP server is a **thin client**: on startup it checks for a running daemon and,
if there isn't one, spawns it detached (the menu-bar **tray** on macOS, which
hosts the daemon; a bare `dispatch-daemon` elsewhere). It then drives that daemon
over its loopback API - it never opens its own broker connection.

```bash
# 1. Install the package so the plugin's commands (dispatch-mcp, dispatch,
#    dispatch-daemon) are on your PATH:
pipx install git+https://github.com/kaan7305/dispatch.git

# 2. Sign in - entirely from the terminal (opens one browser tab to approve):
dispatch login --broker https://your-broker
#    Opens the broker sign-in, you confirm the shown code, and the JWT +
#    broker URL are saved to ~/.dispatch/config.json. No token to copy/paste.
#    (after the first run, plain `dispatch login` reuses the saved broker.)

# 3. Add the marketplace and install the plugin (one-time), in Claude Code:
/plugin marketplace add kaan7305/dispatch
/plugin install dispatch@dispatch
```

You don't need a separate Anthropic API key for in-session mode - the agent
runs on your existing Claude Code login. Restart your Claude Code session;
the `dispatch-mcp` server starts with each session and exposes the tools it
drives:

```
dispatch_read(what) - inbox | status | sent | contacts | invitations | approvals | whoami
dispatch_act(action,…) - accept | decline | cancel | approve | deny
dispatch_send(…) - send a dispatch
dispatch_invite(action) - send | list | accept | decline (invitations)
dispatch_trust(action) - revoke | edit an existing trust edge's scopes
```
(`dispatch_act(action="accept", …)` runs the accepted task in the sandboxed
dp-agent and prompts you inline for each tool call.)

Accept/decline and per-tool approvals (Layer 3) are surfaced here via inline
prompts but **resolved against your daemon** - the daemon (not the broker) holds
the approval futures, and it *ignores* any decision relayed over the broker WS,
so a compromised broker still can't fabricate your consent. The device key and
executor live in the daemon; Layers 2 and 3 are unchanged.

Because the daemon persists across sessions, dispatches are received even when no
Claude session is open, and what one session accepts is visible in another.
Anything sent while *no* daemon is running waits in the broker's offline queue
(an SMS nudges you) and lands when a daemon next comes up. For guaranteed
always-on reachability or scheduled runs without relying on a session to spawn
it, install the daemon directly (below).

### B) Always-on daemon - for background / scheduled use

Everyone - sender and recipient - runs a daemon. After signing in to the
broker, the page shows a one-line installer:

```bash
curl -fsSL https://your-broker/install.sh | bash -s -- <your-token>
```

To set your Anthropic API key in the same step (the recipient runs the agent
on their own key), pass it as a second argument:

```bash
curl -fsSL https://your-broker/install.sh | bash -s -- <your-token> sk-ant-...
```

It installs `pipx` if needed, installs the `dispatch-daemon` command, saves
broker + token (and the API key, if given) to `~/.dispatch/config.json`, and
starts the daemon. On first run it generates this machine's Ed25519 device
keypair and enrolls the public key with the broker. Every later run is just:

```bash
dispatch-daemon
```

Manual install: `pipx install git+<repo>` then `dispatch-daemon --broker URL
--token JWT`. `python -m dispatch.daemon ...` also works in a venv.

---

## Claude Code skill (`/dispatch`)

Drive Dispatch from inside Claude Code with natural language - "dispatch this
to Edward", "what's in my dispatch inbox?", "accept that dispatch" - or the
`/dispatch` slash command. The skill is pure instructions; the real work runs
through the `dispatch` CLI it calls.

Two pieces ship with the package:

1. **`dispatch` CLI** - a thin terminal client for the broker, installed as
   the `dispatch` command alongside `dispatch-daemon` (entry point
   `dispatch.cli:main`). It reads the broker URL + JWT the daemon already saved
   to `~/.dispatch/config.json`, so once your daemon is set up there's nothing
   else to configure.

   ```
   # Setup / lifecycle:
   dispatch login [--broker URL]            # device-code sign-in; saves config
   dispatch update                          # update the package (+ plugin if changed)
   dispatch tray                            # launch the menu-bar tray (hosts the daemon)

   # Broker-backed:
   dispatch whoami                          # who am I + which broker
   dispatch contacts                        # trust edges + scopes (tools, MCP, approval)
   dispatch send <recipient> '<task>'       # create a dispatch (your daemon signs it)
     [--expires <s>] [--cwd <dir>] [--meta k=v]
   dispatch sent                            # dispatches I've sent
   dispatch inbox                           # dispatches addressed to me
   dispatch status <id>                     # one dispatch: status + event trace
   dispatch cancel <id>                     # cancel an in-flight dispatch (either party)
   dispatch invite <email>                  # invite someone to dispatch to you
   dispatch set-scope <edge> [--tools …]    # edit an edge's permissions
     [--mcp gog,notion | '*'] [--paths …] [--approval manual|auto]
   dispatch revoke <edge>                   # revoke an edge (cancels in-flight)

   # Resolved by THIS machine's daemon (loopback API, not the broker):
   dispatch accept <id> | decline <id>      # decide on an inbound dispatch
   dispatch approvals                       # tool calls awaiting allow/deny
   dispatch approve <id> <request_id>       # allow one pending tool call
   dispatch deny <id> <request_id>          # deny one pending tool call
   ```

   Add `--json` to any command for machine-readable output (works before or
   after the subcommand). `--broker` / `--token` (or `$DISPATCH_BROKER` /
   `$DISPATCH_TOKEN`) override the saved config. `recipient` is the contact's
   user id as shown under `peer` in `dispatch contacts`. The CLI never holds a
   signing key: `send` needs *your* daemon online to sign (Layer 2), and
   `accept`/`approve` hit your daemon's loopback API (`127.0.0.1`, token at
   `~/.dispatch/local.token`) - the daemon **ignores** decisions relayed by the
   broker so a compromised broker can't fabricate your approval. Accepting a
   dispatch is **not** blanket approval: under a `manual` edge each tool call
   still needs `dispatch approve`/`deny` (or a click in the local UI).

2. **The skill** - `skills/dispatch/SKILL.md`. Symlink it into Claude Code:

   ```bash
   mkdir -p ~/.claude/skills
   ln -sfn "$PWD/skills/dispatch" ~/.claude/skills/dispatch
   ```

   Confirm with `ls ~/.claude/skills/dispatch/SKILL.md`. After that, Claude
   auto-invokes it on the trigger phrases above and walks you through the
   send / inbox / accept flows, always confirming before it sends or accepts.
   Tool-call approvals (Layer 3) stay in the daemon's local approval UI - the
   skill points you there rather than approving on your behalf.

---

## Using it

1. **Sign in** - the web UI signs you in via Clerk (Google); the CLI/daemon use
   `dispatch login` (a device-code flow: it opens the browser, you confirm a
   code, and the JWT is saved to `~/.dispatch/config.json`).
2. **Run your daemon** - the install one-liner once, or just open Claude Code
   with the plugin and let it auto-spawn the daemon.
3. **Invite** - in Contacts (or `dispatch invite <email>`), invite a teammate.
   They get an emailed link.
4. **They accept** - opening the link (or via `dispatch_invite`), they choose the
   scopes they grant you: built-in tools, which of their MCP servers, and
   approval mode. Now a trust edge `you → them` exists. They can change it later
   with `set-scope` / Edit permissions, or `revoke` it.
5. **Compose** - pick them as recipient, describe the task, send.
   - Layer 1: the broker checks the edge, scope, and rate limit.
   - The broker asks *your* daemon to sign the dispatch.
   - Layer 2: their daemon verifies the signature before surfacing it.
6. **They accept the dispatch** in their Inbox; the agent runs, restricted to
   the edge's tools and paths. Destructive calls prompt them (Layer 3) unless
   the edge is `approval: auto`.
7. **Watch** - the full event stream (reasoning, tool calls, results,
   approvals) appears live in your Sent card and their Inbox card.

Revoke a contact any time (`Revoke` in Contacts) - it cancels anything
in flight on that edge and refuses new dispatches immediately.

---

## API

### HTTP

| Verb | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/clerk` | – | exchange a Clerk token for a Dispatch JWT (web UI) |
| POST | `/auth/device` → `/approve` → `/token` | – / Bearer | device-code flow for the CLI (`dispatch login`) |
| POST | `/auth/login` | – | dev/CLI login (username → JWT) |
| POST | `/auth/signout` | Bearer | sign out |
| GET | `/install.sh` | – | the daemon installer script |
| GET | `/health` | – | liveness + DB check |
| GET | `/me`, `/me/phone` · POST `/me/phone` | Bearer | identity / SMS-notify number |
| POST | `/devices/enroll` | Bearer | register a device public key |
| GET / PATCH / DELETE | `/devices`, `/devices/{id}` | Bearer | list / rename / revoke devices |
| POST / GET | `/invitations` | Bearer | send / list invitations |
| GET | `/invitations/{token}` | – | invitation detail |
| POST | `/invitations/{token}/accept` \| `/decline` | Bearer | resolve an invitation |
| GET | `/trust` | Bearer | my contacts (accepted edges + scopes) |
| PATCH / DELETE | `/trust/{id}` | Bearer | edit scopes (trustor only) / revoke |
| POST | `/dispatch` · `/dispatch/{id}/cancel` | Bearer | create / cancel a dispatch |
| GET | `/dispatch/{id}`, `/dispatches` | Bearer | record / history |
| GET | `/app`, `/app/{path}` | – | the React web UI (SPA) |

### WebSocket

- `/agent/connect?token=` - the daemon. First frame: `{type:"hello",device_id}`.
  Broker → daemon: `new_dispatch`, `sign_request`, `cancel_dispatch`. Daemon →
  broker: `signed`, `dispatch_status`, `dispatch_event`. **Approval/decision
  frames are deliberately *not* on this socket** - Layer 3 resolves on the
  daemon's own loopback API, and the daemon ignores any decision relayed by the
  broker, so the broker can't fabricate consent.
- `/inbox?token=` - the recipient's browser. Streams inbox + per-dispatch
  events; still accepts `dispatch_decision` / `tool_approval` for the
  browser-driven path, but the recipient's daemon only honors decisions made
  against its own local API, not these.
- `/dispatch/{id}/watch?token=` - the sender's browser; streams events.

### Signing

The signed canonical payload (`shared/signing.py`, deterministic sorted-key
JSON) covers: `instruction`, `sender_device`, `recipient_user`,
`target_device`, `nonce`, `created_at`. The sender's daemon signs it
(Ed25519); the recipient's daemon rebuilds the identical bytes and verifies
against the sender device's public key - **pinned on first sight (TOFU)** in
`~/.dispatch/pins.json`, so the broker can't swap a key later. Replay is caught
by a **durable** per-`(device,nonce)` guard (`~/.dispatch/nonces.db`, persisted
across restarts); a wide 30-day freshness window then bounds staleness while
still letting a dispatch wait in the offline queue for a recipient who's away.

---

## Project layout

```
src/dispatch/
  cli.py          dispatch - terminal client (broker + loopback daemon API)
  mcp_server.py   dispatch-mcp - in-session thin client; ensures + drives the daemon
  shared/
    schema.py     DispatchPayload, DispatchEvent, DispatchStatus, Scopes, …
    identity.py   JWT issue/verify (HS256)
    crypto.py     Ed25519 keypair / sign / verify (PyNaCl)
    signing.py    canonical dispatch payload - what a signature covers
  executor/
    executor.py   run_dispatch() - opens the agent session, scoped tools/MCP
  broker/
    schema.sql    Postgres tables (idempotent)
    store.py      all SQL (asyncpg): users, devices, trust, dispatches, …
    state.py      runtime-only WS connections
    email.py      invitation email (Resend, or dev console)
    clerk.py      Clerk token verification (web sign-in)
    app.py        FastAPI broker - auth, devices, trust, dispatch, WS, SPA
  daemon/
    main.py       dispatch-daemon - broker WS client, signer, verifier, runner
    local_app.py  the daemon's 127.0.0.1 API + served web UI (inbox, approvals, trust)
    connlock.py   advisory lock: one broker connection per machine
    nonces.py     durable replay-nonce store
    identity.py   device keypair, keychain, enrollment, key pins
  tray/
    app.py        dispatch-tray - macOS menu-bar indicator that hosts the daemon
  web/desktop/    the React web UI (served by the broker and each daemon)
```

---

## Security model & honest limitations

What the design **does** guarantee:

- No dispatch runs without an accepted, in-scope, unexpired trust edge.
- A dispatch is cryptographically tied to a device of the named sender; a
  compromised broker cannot forge a sender or swap a pinned key.
- The agent is confined to the edge's tools and (when set) path allowlist.
- Revocation is immediate and cancels in-flight work.

What it **does not** - known limitations, not papered over:

- **Signing proves origin, not authorship.** Because composing happens in the
  browser and the daemon signs what's relayed, a fully-compromised broker
  could get a sender's daemon to sign a task the sender didn't type. Closing
  this needs a trusted compose surface (composing in the daemon) - the design
  doc's Phase 7 / §14 item.
- **Key pinning is TOFU.** A broker compromised *before* a device's first
  contact could seed a wrong key. Pin-at-trust-establishment would harden it.
- **`DELETE /devices`** drops the device's connection (stopping its running
  agents) but doesn't re-status individual dispatches - no per-dispatch
  device tracking. `DELETE /trust` *is* precise.
- **`Bash` is not path-checkable.** The `paths` allowlist is enforced on
  `Read/Write/Edit/Glob/Grep`; granting `Bash` grants shell, full stop.
- **No end-to-end encryption.** The broker relays plaintext instructions.
  E2E (libsodium sealed boxes) is deferred (Phase 7).
- The replay-nonce guard is durable (sqlite, survives restarts), so the 30-day
  freshness window doesn't widen replay exposure - but that window does mean a
  signed dispatch stays valid for delivery up to 30 days, by design (offline
  queue).

---

## Troubleshooting

- **`ModuleNotFoundError: dispatch`** - broker: run with `--app-dir src`;
  daemon: `python -m dispatch.daemon` from the repo root, or use the
  installed `dispatch-daemon`.
- **`database: down` from `/health`** - `DATABASE_URL` unset or unreachable.
- **Daemon `handshake failed` / 1008** - token invalid/expired, or the broker
  has a different `DISPATCH_JWT_SECRET` than issued the token.
- **`POST /dispatch` → 403** - no accepted trust edge to that recipient.
  Invite them and have them accept first.
- **`POST /dispatch` → 503** - your own daemon is offline; it has to sign.
- **Dispatch rejected with `SignatureRejected`** - signature/nonce/freshness
  failed, or the sender device's key changed since it was pinned.
- **Recipient's inbox empty** - their daemon must be connected and they must
  be signed in to the broker as the same user.
