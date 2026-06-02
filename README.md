# Dispatch

A peer-to-peer courier for **agentic work**. One person describes a task in
natural language and names a recipient; the recipient's machine runs it with
a local AI agent — but only if the two people have an explicit, scoped,
revocable **trust relationship**, the dispatch is **cryptographically signed**
by the sender's device, and the recipient **approves** it.

A dispatch never runs on a machine unless all three hold:

1. an accepted **trust edge** exists from sender to recipient,
2. the dispatch carries a valid **signature** from a device of that sender,
3. it stays within the **scope** that edge grants — and destructive tool
   calls get a per-call human **approval**.

---

## Components

| Component | What it is |
|---|---|
| **Broker** | Multi-tenant FastAPI service, Postgres-backed. Issues identity, routes dispatches, enforces trust policy, relays events. Never holds a signing key; never touches a recipient's filesystem. |
| **Daemon** (`dispatch-daemon`) | A small background process each user runs on their own machine. Holds that machine's Ed25519 device key, signs the user's outgoing dispatches, verifies incoming ones, and runs the agent. |
| **Web UI** | Served by the broker. One page: sign in, manage contacts, compose, watch sent dispatches, act on your inbox. |
| **Agent** | A Claude Agent SDK session the daemon opens per accepted dispatch. Transient — created on accept, gone when the task ends. |

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

- **User** — identified by email. Created on first sign-in.
- **Device** — one per machine running the daemon. Has an Ed25519 keypair;
  the private key never leaves the machine (OS keychain, or a 0600 file).
- **Invitation** — an emailed, single-use, expiring link. How a trust edge
  is born.
- **Trust edge** — a directed `from_user → to_user` relationship. Created
  when the recipient accepts an invitation. Per-direction: B accepting A's
  invite lets *A dispatch to B*, nothing more.
- **Scopes** — what an edge permits, set by the recipient (the trustor):
  ```json
  {
    "tools": ["Read", "Glob", "Grep"],     // subset of Read/Write/Edit/Bash/Glob/Grep
    "paths": ["~/work"],                   // file-path allowlist ([] = no path limit)
    "approval": "manual",                  // "manual" = approve every tool call | "auto"
    "max_dispatches_per_day": 50,
    "expires_at": null
  }
  ```
  New edges default to least privilege: read-only tools, manual approval.
- **Dispatch** — one task. Signed by the sender's device, authorized by an
  edge, executed by the recipient's daemon.

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
   is auto-injected — the broker uses it as its public URL, so magic links
   and the install one-liner are correct without extra config.
4. Settings → Networking → Generate Domain. `/health` is the healthcheck.

The broker schema is idempotent (`broker/schema.sql`, `CREATE TABLE IF NOT
EXISTS` + `ADD COLUMN IF NOT EXISTS`) and runs on every startup — no
migration step.

### Environment

| Var | Where | Notes |
|---|---|---|
| `DISPATCH_JWT_SECRET` | Broker | 32+ random chars. Rotating it invalidates all tokens. |
| `DATABASE_URL` | Broker | Postgres URL. Auto-set by Railway's plugin. |
| `DISPATCH_PUBLIC_URL` | Broker (optional) | Public base URL; falls back to `RAILWAY_PUBLIC_DOMAIN`, then localhost. |
| `RESEND_API_KEY`, `RESEND_FROM` | Broker (optional) | Real email for magic links + invitations. Without it, links are returned in the API response (dev mode). |
| `DISPATCH_DAEMON_INSTALL` | Broker (optional) | What `/install.sh` points `pipx install` at. Default: a GitHub repo URL. |
| `ANTHROPIC_API_KEY` | Daemon | The recipient runs the agent on their own key. |
| `DISPATCH_WORKSPACE` | Daemon (optional) | Agent working directory. Default `./workspace`. |
| `DISPATCH_HOME` | Daemon (optional) | Where config + device key live. Default `~/.dispatch`. |
| `DISPATCH_KEY_BACKEND` | Daemon (optional) | `file` to store the device key in a 0600 file instead of the OS keychain. |

---

## Installing the daemon

Everyone — sender and recipient — runs a daemon. After signing in to the
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

Drive Dispatch from inside Claude Code with natural language — "dispatch this
to Edward", "what's in my dispatch inbox?", "accept that dispatch" — or the
`/dispatch` slash command. The skill is pure instructions; the real work runs
through the `dispatch` CLI it calls.

Two pieces ship with the package:

1. **`dispatch` CLI** — a thin terminal client for the broker, installed as
   the `dispatch` command alongside `dispatch-daemon` (entry point
   `dispatch.cli:main`). It reads the broker URL + JWT the daemon already saved
   to `~/.dispatch/config.json`, so once your daemon is set up there's nothing
   else to configure.

   ```
   dispatch whoami                          # who am I + which broker
   dispatch contacts                        # trust edges: who can dispatch to whom
   dispatch send <recipient> '<task>'       # create a dispatch (daemon must be online to sign)
     [--expires <s>] [--cwd <dir>] [--meta k=v]
   dispatch sent                            # dispatches I've sent
   dispatch inbox                           # dispatches addressed to me
   dispatch status <id>                     # one dispatch: status + event trace
   dispatch accept <id> | decline <id>      # decide on an inbound dispatch (daemon online)
   dispatch cancel <id>                     # cancel an in-flight dispatch (either party)
   ```

   Add `--json` to any command for machine-readable output (works before or
   after the subcommand). `--broker` / `--token` (or `$DISPATCH_BROKER` /
   `$DISPATCH_TOKEN`) override the saved config. `recipient` is the contact's
   user id as shown under `peer` in `dispatch contacts`. The CLI is a client
   only — it never holds a signing key; `send`/`accept` still require *your*
   daemon online so the signature (Layer 2) and the agent run on your machine,
   not the broker.

2. **The skill** — `skills/dispatch/SKILL.md`. Symlink it into Claude Code:

   ```bash
   mkdir -p ~/.claude/skills
   ln -sfn "$PWD/skills/dispatch" ~/.claude/skills/dispatch
   ```

   Confirm with `ls ~/.claude/skills/dispatch/SKILL.md`. After that, Claude
   auto-invokes it on the trigger phrases above and walks you through the
   send / inbox / accept flows, always confirming before it sends or accepts.
   Tool-call approvals (Layer 3) stay in the daemon's local approval UI — the
   skill points you there rather than approving on your behalf.

---

## Using it

1. **Sign in** — enter your email, click the magic link.
2. **Run your daemon** — the install one-liner, once.
3. **Invite** — in Contacts, invite a teammate by email. They get a link.
4. **They accept** — opening the link, they choose the scopes they grant you
   (tools, approval mode) and accept. Now a trust edge `you → them` exists.
5. **Compose** — pick them as recipient, describe the task, send.
   - Layer 1: the broker checks the edge, scope, and rate limit.
   - The broker asks *your* daemon to sign the dispatch.
   - Layer 2: their daemon verifies the signature before surfacing it.
6. **They accept the dispatch** in their Inbox; the agent runs, restricted to
   the edge's tools and paths. Destructive calls prompt them (Layer 3) unless
   the edge is `approval: auto`.
7. **Watch** — the full event stream (reasoning, tool calls, results,
   approvals) appears live in your Sent card and their Inbox card.

Revoke a contact any time (`Revoke` in Contacts) — it cancels anything
in flight on that edge and refuses new dispatches immediately.

---

## API

### HTTP

| Verb | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/request` | – | email a magic link |
| GET | `/auth/magic?token=` | – | exchange link for a JWT |
| POST | `/auth/login` | – | dev/CLI login (username → JWT) |
| GET | `/install.sh` | – | the daemon installer script |
| GET | `/health` | – | liveness + DB check |
| POST | `/devices/enroll` | Bearer | register a device public key |
| GET / DELETE | `/devices`, `/devices/{id}` | Bearer | list / revoke devices |
| POST / GET | `/invitations` | Bearer | send / list invitations |
| GET | `/invitations/{token}` | – | invitation detail |
| POST | `/invitations/{token}/accept` \| `/decline` | Bearer | resolve an invitation |
| GET | `/trust` | Bearer | my contacts (accepted edges) |
| PATCH / DELETE | `/trust/{id}` | Bearer | edit scopes (trustor only) / revoke |
| POST | `/dispatch` | Bearer | create a dispatch (Layers 1 + signing) |
| GET | `/dispatch/{id}`, `/dispatches` | Bearer | record / history |

### WebSocket

- `/agent/connect?token=` — the daemon. First frame: `{type:"hello",device_id}`.
  Broker → daemon: `new_dispatch`, `sign_request`, `cancel_dispatch`,
  `dispatch_decision`, `tool_approval`. Daemon → broker: `signed`,
  `dispatch_status`, `dispatch_event`.
- `/inbox?token=` — the recipient's browser. Streams inbox + per-dispatch
  events; sends `dispatch_decision` and `tool_approval`.
- `/dispatch/{id}/watch?token=` — the sender's browser; streams events.

### Signing

The signed canonical payload (`shared/signing.py`, deterministic sorted-key
JSON) covers: `instruction`, `sender_device`, `recipient_user`,
`target_device`, `nonce`, `created_at`. The sender's daemon signs it
(Ed25519); the recipient's daemon rebuilds the identical bytes and verifies
against the sender device's public key — **pinned on first sight (TOFU)** in
`~/.dispatch/pins.json`, so the broker can't swap a key later. Replay is
caught by a per-`(device,nonce)` guard; staleness by a 5-minute window.

---

## Project layout

```
src/dispatch/
  cli.py          dispatch — terminal client for the broker (drives the /dispatch skill)
  shared/
    schema.py     DispatchPayload, DispatchEvent, DispatchStatus, Scopes, …
    identity.py   JWT issue/verify (HS256)
    crypto.py     Ed25519 keypair / sign / verify (PyNaCl)
    signing.py    canonical dispatch payload — what a signature covers
  executor/
    executor.py   run_dispatch() — opens the agent session, scoped tools
  broker/
    schema.sql    Postgres tables (idempotent)
    store.py      all SQL (asyncpg): users, devices, trust, dispatches, …
    state.py      runtime-only WS connections
    email.py      magic-link + invitation email (Resend, or dev console)
    app.py        FastAPI broker — auth, devices, trust, dispatch, WS
  daemon/
    main.py       dispatch-daemon — WS client, signer, verifier, runner
    identity.py   device keypair, keychain, enrollment, key pins
  web/app/        the unified web UI
```

---

## Security model & honest limitations

What the design **does** guarantee:

- No dispatch runs without an accepted, in-scope, unexpired trust edge.
- A dispatch is cryptographically tied to a device of the named sender; a
  compromised broker cannot forge a sender or swap a pinned key.
- The agent is confined to the edge's tools and (when set) path allowlist.
- Revocation is immediate and cancels in-flight work.

What it **does not** — known limitations, not papered over:

- **Signing proves origin, not authorship.** Because composing happens in the
  browser and the daemon signs what's relayed, a fully-compromised broker
  could get a sender's daemon to sign a task the sender didn't type. Closing
  this needs a trusted compose surface (composing in the daemon) — the design
  doc's Phase 7 / §14 item.
- **Key pinning is TOFU.** A broker compromised *before* a device's first
  contact could seed a wrong key. Pin-at-trust-establishment would harden it.
- **`DELETE /devices`** drops the device's connection (stopping its running
  agents) but doesn't re-status individual dispatches — no per-dispatch
  device tracking. `DELETE /trust` *is* precise.
- **`Bash` is not path-checkable.** The `paths` allowlist is enforced on
  `Read/Write/Edit/Glob/Grep`; granting `Bash` grants shell, full stop.
- **No end-to-end encryption.** The broker relays plaintext instructions.
  E2E (libsodium sealed boxes) is deferred (Phase 7).
- In-memory replay-nonce set on the daemon resets on restart; the 5-minute
  freshness window bounds the exposure.

---

## Troubleshooting

- **`ModuleNotFoundError: dispatch`** — broker: run with `--app-dir src`;
  daemon: `python -m dispatch.daemon` from the repo root, or use the
  installed `dispatch-daemon`.
- **`database: down` from `/health`** — `DATABASE_URL` unset or unreachable.
- **Daemon `handshake failed` / 1008** — token invalid/expired, or the broker
  has a different `DISPATCH_JWT_SECRET` than issued the token.
- **`POST /dispatch` → 403** — no accepted trust edge to that recipient.
  Invite them and have them accept first.
- **`POST /dispatch` → 503** — your own daemon is offline; it has to sign.
- **Dispatch rejected with `SignatureRejected`** — signature/nonce/freshness
  failed, or the sender device's key changed since it was pinned.
- **Recipient's inbox empty** — their daemon must be connected and they must
  be signed in to the broker as the same user.
