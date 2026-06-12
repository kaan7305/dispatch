# Dispatch - Summary

## What it is

Dispatch is a peer-to-peer courier for agentic work. One person writes a task in natural language and names a recipient; the recipient's own machine runs it with a local Claude agent - but only when three independent conditions all hold:

- an accepted, scoped, revocable **trust edge** exists from sender to recipient,
- the dispatch carries a valid **Ed25519 signature** from one of the sender's devices, and
- the work stays inside the **scope** that edge grants, with tool calls gated by per-call human approval (unless the edge or the specific tool has been pre-approved).

Every participant is both a sender and a recipient, and everyone runs a daemon.

The reason Dispatch is more than "remote shell with extra steps" is the **MCP capability layer**: an edge can grant a sender access to the recipient's *own* powerful tools - their Notion, their search, their GitHub, their domain skills - not just the six built-in file/shell tools. That is what makes a dispatched task useful: it runs with the recipient's capabilities, under the recipient's control.

## Components

- **Broker** - a multi-tenant FastAPI service backed by Postgres. It issues identity, routes dispatches, enforces trust policy, and relays live events. It never holds a signing key and never touches a recipient's filesystem.
- **Daemon (`dispatch-daemon`)** - a small background process each user runs on their own machine. It holds that machine's Ed25519 device key, signs outgoing dispatches, verifies incoming ones, runs the agent, discovers and scopes the machine's MCP servers, and serves a local approval UI on `127.0.0.1`.
- **Claude plugin (`dispatch`)** - an installable Claude Code plugin (`.claude-plugin/plugin.json`) that ships a `/dispatch` skill and an in-session stdio MCP server, `dispatch-mcp`. This is the natural-language front door: you delegate, check your inbox, and approve from inside an ordinary Claude session. `dispatch-mcp` is a *thin client of the local daemon* - it holds no broker connection, device key, or executor; it ensures a daemon is running (spawning one if needed) and talks to it over the `127.0.0.1` API.
- **CLI (`dispatch`)** - a terminal client of the broker HTTP API + local daemon. It's both a direct human interface and the command surface the `/dispatch` skill drives under the hood. Every command takes `--json`.
- **Web UI** - served by the broker: sign in, manage contacts, compose tasks, watch sent dispatches, and act on the inbox.
- **Desktop app + tray** - a packaged React/Vite client (with a macOS menu-bar agent) wrapping the same flows in native windows, plus device management and native notifications.
- **Agent** - a Claude Agent SDK session the daemon opens per accepted dispatch, running on the recipient's own `ANTHROPIC_API_KEY`, confined to the edge's tools, MCP grants, and paths. It is transient - created on accept, gone when the task ends. It defaults to Claude Sonnet (`claude-sonnet-4-6`, overridable via `DISPATCH_EXECUTOR_MODEL`).

## Core concepts

- **User** - identified by email, created on first sign-in.
- **Device** - one per machine; has an Ed25519 keypair whose private key never leaves the machine (OS keychain, or a `0600` file).
- **Invitation** - an emailed, single-use, expiring link; how a trust edge is born.
- **Trust edge** - a directed `from_user → to_user` relationship created when the recipient accepts an invitation. It is per-direction: B accepting A's invite lets A dispatch to B, nothing more.
- **Scopes** - what an edge permits, set by the recipient (the trustor). New edges default to least privilege. The fields:
  - **`tools`** - a built-in allowlist, a subset of `Read / Write / Edit / Bash / Glob / Grep`. **Default: `Read / Glob / Grep`** (read-only).
  - **`mcp`** - an allowlist of the recipient's *own* MCP servers/tools the sender may use. Patterns: `"notion"` (any tool from the `notion` server), `"mcp__github__*"` (any tool from `github`), `"mcp__search__web"` (one exact tool). **Default: empty** (no MCP).
  - **`auto_tools`** - exact tool names (built-in like `Bash`, or full MCP like `mcp__notion__notion-move-pages`) that skip the per-call approval prompt. Grown just-in-time when the recipient picks "Always allow this tool" on a live approval. Orthogonal to `mcp`: `mcp` decides what's *reachable*; `auto_tools` decides what no longer needs a human *Allow*.
  - **`paths`** - an optional path allowlist for file tools.
  - **`approval`** - `manual` (approve every tool call) or `auto`.
  - **`max_dispatches_per_day`** - a daily rate cap (default 50).
  - **`expires_at`** - an optional edge expiry.
  - **`sync`** - optional standing permission for read-only context digests (see *Context sync*, below). `None` by default.
- **Skills** are *not* scoped here. A Skill is just instructions/context and grants no capability on its own - anything it tries to use still passes through the `tools` / `mcp` / `paths` gate. So a delegated task inherits all of the recipient's Skills, but the real sandbox is the tool and MCP allowlists.
- **Dispatch** - one task: signed by the sender's device, authorized by an edge, executed by the recipient's daemon.

The recipient's *dispatch control plane* (sending, inviting, approving) is never grantable through an edge - a dispatched task cannot re-wield the recipient's identity to dispatch onward. That exclusion is enforced in the executor, not by the allowlist.

## The three enforcement layers

Trust is checked in three independent places, so a compromised broker cannot forge a trusted sender:

- **Layer 1 - Trust policy (broker):** Is there an accepted edge? Is the request in scope? Under the daily rate limit? Otherwise 403 / 429.
- **Layer 2 - Signature (recipient's daemon):** Is the signature valid, the device key pinned, the nonce fresh and unseen? This runs entirely on the recipient's machine against a key the broker cannot substitute. Otherwise rejected.
- **Layer 3 - Human approval (recipient):** Does the human approve this specific tool call? Resolved only on the recipient's own machine. For each call the recipient can answer **Allow** (this call), **Deny**, **Always allow this tool** (persists onto the edge's `auto_tools`), or **Allow for this session** (until the daemon restarts). An edge in `auto` mode, or a tool already in `auto_tools`, skips the prompt.

## How MCP scoping works (the capability layer)

This is the newest and most important addition, and it has two tiers by design:

1. **Server reachability - granted up front.** When the recipient accepts an invitation (or edits permissions later), they choose which of their installed MCP servers a sender may reach, via the `mcp` allowlist. The daemon discovers installed servers from the recipient's Claude configuration across user, project, and local scopes. There can be dozens of tools per server, so the accept screen grants at **server** granularity, not tool-by-tool.

2. **Powerful tools - approved just-in-time at first use.** Granting a server makes its tools *reachable*, not *automatic*. The first time a dispatched task actually calls a sensitive MCP tool - say `notion-move-pages` versus the harmless `notion-get-comments` - the recipient gets a live prompt: **Allow / Deny / Always allow this tool / Allow this session.** "Always" writes that exact tool into the edge's `auto_tools` (persisted via a `PATCH /trust/{id}` straight from the daemon mid-run) so it never asks again; "this session" remembers it only until the daemon restarts. An edge set fully `auto` is the "give this contact access to everything, no manual prompts" path.

At execution time the executor exposes only the scoped MCP servers (`strict_mcp_config`), and **fast-fails** a dispatch whose required MCP server is dead or unreachable rather than silently running without it. Every MCP tool call still passes the same `can_use_tool` gate as a built-in.

## How the communication channel works

The system is built around one persistent WebSocket per daemon to the broker, plus per-view browser sockets. The broker is a relay, not a participant - it owns no keys and resolves no user intent.

**1. Daemon ↔ broker (the spine).** Each daemon opens a long-lived WebSocket to the broker at `/agent/connect?token=<JWT>` (`ws://` for local dev, `wss://` in production, verified with certifi's CA bundle). Its first frame is `{type:"hello", device_id}`. The broker keeps live connections in runtime-only state: a map of `user_id → {device_id → WebSocket}` for daemons, plus browser watchers per dispatch and per inbox. None of this survives a restart - everything durable (users, devices, trust, dispatches, events) lives in Postgres. To route to a user who hasn't named a specific device, the broker picks that user's most-recently-connected device (dict insertion order tracks recency).

Message types over this socket:

- **Broker → daemon:** `sign_request` (asking the sender's device to sign an outgoing dispatch), `new_dispatch` (delivering an incoming task), `cancel_dispatch` (a trust edge was revoked - stop now), `signed_out` (clear the cached token), and `error` (why the broker is about to close the socket). *(An `approval_decision` frame for remote signed approvals is in progress - see Partial features.)*
- **Daemon → broker:** `signed` (the requested signature), `dispatch_status` (`pending` / `delivered` / `accepted` / `running` / `completed` / …), and `dispatch_event` (every agent step - reasoning text, tool use, tool result, permission request/response - streamed live).

**2. The signing round-trip.** Composition happens in the browser. When a user sends a task, the broker runs Layer 1, then sends a `sign_request` down the sender's own daemon socket. The daemon rebuilds the canonical payload - a deterministic, sorted-key, compact JSON object over `instruction, sender_device, recipient_user, target_device, nonce, created_at` - signs those exact bytes with its Ed25519 private key, and returns `signed`. The broker attaches the signature and relays a `new_dispatch` to the recipient.

If the **sender's daemon is offline**, the dispatch can't be signed yet: the broker stores it as `awaiting_signature` and queues it, then signs and releases it automatically when that daemon reconnects (rather than hard-failing the send).

**3. Verification on arrival (Layer 2).** On `new_dispatch`, the recipient's daemon rebuilds the identical canonical bytes and checks, in order: a complete signing block is present; the dispatch is fresh (signed within a 5-minute window); the nonce has not been seen this session (replay guard); the sender device's public key matches the locally pinned key - pinned on first sight (TOFU) in `~/.dispatch/pins.json`, so the broker cannot swap a key later; and finally the Ed25519 signature verifies. Any failure → rejected with a `SignatureRejected` error event, never surfaced to the user.

**4. The local approval channel (Layer 3).** This is the key trust boundary: the daemon runs its own FastAPI server on `127.0.0.1`, and that local server is the only surface that resolves user-intent decisions - top-level Accept/Reject and per-tool Allow/Deny/Always/Session. The broker WebSocket does not carry the *authority* to resolve an approval; user intent is decided locally. When a manual call occurs, the daemon parks an `asyncio.Future`, shows the request in the local UI, and waits (120 s timeout → deny); top-level decisions have a 300 s timeout → expire.

**5. Browser event streams.** Two more WebSocket endpoints fan events out to humans: `/inbox?token=` streams the recipient's inbox and per-dispatch events (and carries their decisions to the daemon's local server), and `/dispatch/{id}/watch?token=` streams the same events to the sender's browser so they can watch reasoning, tool calls, results, and approvals live. Every executor event is mirrored both upstream (to the broker, for the sender's watch view) and into local state (for the recipient's local UI).

**6. Path & tool enforcement at execution.** As the agent runs, each tool call passes through a `can_use_tool` callback: the tool (built-in or MCP) must be in scope, and for `Read / Write / Edit / Glob / Grep` any path argument is resolved and checked against the path allowlist. `Bash` is not path-checkable, so it is gated wholesale by being in the tool list. Out-of-scope calls auto-deny; in-scope calls either auto-allow (`auto` mode, or a tool in `auto_tools`) or wait for the human.

**7. Workflows.** Dispatches can carry a `metadata.workflow` envelope describing an n8n-style graph (agent / AI / data / branch / HTTP / cron / code / delay / end nodes, with multi-recipient fan-out). Workflow dispatches skip the top-level Accept and run unattended - the reasoning being that if the recipient trusted the sender enough to receive the workflow, every step inherits that trust - but the tool allowlist, MCP grants, and path restrictions still fully apply. A `WorkflowEngine` in the daemon walks the graph, reusing the same `can_use_tool` callback for every agent node, and PATCHes run state back to the broker; a `CronScheduler` fires scheduled runs.

## Context sync (read-only activity digests)

`dispatch sync` lets a trusted contact pull a **read-only digest** of a machine's recent Claude Code activity - what projects were worked on, recent sessions - without running any sender-authored code. It is deliberately separate from the dispatch tool scope:

- A sync never runs free text. The recipient's daemon runs a **fixed, read-only digest template**, so it can be auto-approved without granting the sender arbitrary execution.
- The grant (`SyncScope`) is recipient-controlled: which directories of transcripts are readable (`roots`, default `~/.claude/projects`), an optional project allowlist, a maximum look-back window, and whether it runs unattended.
- A sender's request may only ever **narrow** the grant (a shorter window, a subset of projects) - never widen `roots`. Since dispatch metadata isn't covered by the signature, this recipient-side clamp is what bounds a tampered request.
- Granting dispatch does **not** grant sync - `Scopes.sync` stays `None` until the trustor explicitly enables it (`dispatch sync-grant`).

It is a newer, narrow feature: a one-directional digest pull, not a live two-way context-sharing channel (see Partial features).

## Interfaces - how you drive Dispatch

There are several surfaces, and the two most commonly used are the ones you'd least expect from a "web app" framing - the Claude plugin and the CLI. All of them ultimately talk to the same daemon + broker; they differ only in ergonomics and which screen you happen to be on.

- **Claude plugin / `/dispatch` skill (in-session, natural language).** The primary front door for everyday use. Installed as a Claude Code plugin, it loads a `dispatch-mcp` MCP server exposing a handful of tools - `dispatch_read` (inbox/status), `dispatch_act` (accept/decline/approve), `dispatch_send`, `dispatch_sync`, `dispatch_invite`, `dispatch_trust` - and a skill that triggers on phrases like *"dispatch this to Edward,"* *"what's in my dispatch inbox?"*, *"accept that invitation,"* or an explicit `/dispatch`. It runs in your existing session (no separate terminal), ensures a daemon is up, and surfaces per-tool approvals inline via `ctx.elicit`, resolved against the daemon - so even here the broker never holds consent. Pre-login it stays dormant (tools tell you to run `dispatch login`); it never spawns anything before you've signed in.
- **CLI (`dispatch …`).** A terminal client of the broker HTTP API and the local daemon - and the command surface the `/dispatch` skill itself drives, so it's exercised constantly whether or not you type it by hand. It covers the full lifecycle: `login`, `whoami`, `doctor`, `contacts`, `invite` / `invitations` / `accept-invitation` / `decline-invitation`, `set-scope` (tools **and** MCP), `sync` / `sync-grant`, `send`, `sent`, `inbox`, `status`, `cancel`, the locally-resolved `accept` / `decline` / `approvals` / `approve` / `deny`, `approve-remote` (signed remote approval), `revoke`, plus `tray` and `update`. Resolution order for broker/token matches the daemon (flag → env → `~/.dispatch/config.json`), and every command takes `--json` for machine-readable output.
- **Broker-served web app.** The single page every user signs in to. Covers the whole lifecycle: Contacts (invite, set/edit the scopes you grant - tools, MCP servers, paths, approval mode, rate limit, expiry - and revoke), Compose, a Sent view streaming each dispatch's live event feed, and an Inbox for acting on incoming work. The Inbox shows an honest scope badge of what the edge actually grants, and the Edit-Permissions dialog manages per-server MCP grants and the list of always-allowed tools.
- **Desktop app + system tray.** A packaged React/Vite desktop client (with a macOS tray agent) wrapping the same flows in native windows: Inbox, History, People/Contacts, Devices, Saved, and a per-dispatch Detail view with a live EventStream. Dialogs handle Compose, Invite, Enroll Device, and Edit Permissions. A live approval card offers Allow / Deny / Allow this session / Always allow this tool. The tray shows live daemon status (enrolling / connecting / connected / disconnected) and posts native notifications when a dispatch arrives or a tool call needs approval. (A single-instance lock keeps the tray from spawning a duplicate icon; a stale daemon is surfaced and safely restarted.)
- **Local approval UI (`127.0.0.1`).** Not a general-purpose interface but a trust boundary: served by the daemon itself and the only place Accept/Reject and Allow/Deny/Always/Session are actually decided, so approvals never depend on the broker being honest. The web, desktop, CLI, and plugin all route user-intent decisions through it.

A note on how these relate: the **daemon owns the single broker connection per machine** (guarded by a connection lock), so the plugin and CLI are thin clients of it rather than competing daemons. That gives cross-session visibility for free - what one terminal or session accepts is immediately visible in another - and keeps the security layers in one place. All views update live over WebSockets; new edges default to read-only tools, no MCP, and manual approval, so the recipient stays in control until they widen scope.

## User setup

1. **Sign in** - open the broker's web page, enter your email, click the magic link (in dev mode without an email provider, the link is returned in the API response).
2. **Install and run your daemon** - the signed-in page shows a one-line installer: `curl -fsSL https://your-broker/install.sh | bash -s -- <your-token>`. It installs pipx if needed, installs the `dispatch-daemon` command, saves broker + token to `~/.dispatch/config.json`, generates this machine's Ed25519 device keypair on first run, and enrolls the public key with the broker. After that, a bare `dispatch-daemon` reconnects from saved config. The recipient supplies their own `ANTHROPIC_API_KEY` (the agent runs on their key, never the broker's).
3. **Invite a teammate** - in Contacts, invite by email; they get a link.
4. **They accept and set scopes** - opening the link, the recipient chooses what they grant (built-in tools, which MCP servers, path allowlist, approval mode, rate limit, expiry) and accepts. Now a trust edge `you → them` exists.
5. **Compose and send** - pick them as recipient, describe the task. The broker checks edge/scope/rate limit (Layer 1), asks your daemon to sign it, and relays it; their daemon verifies the signature (Layer 2) before surfacing it.
6. **They accept the dispatch** in their Inbox; the agent runs, restricted to the edge's tools, MCP grants, and paths, with sensitive calls prompting them (Layer 3) unless pre-approved.
7. **Watch and revoke** - both sides see the live event stream. Revoking a contact is immediate: it cancels anything in flight on that edge and refuses new dispatches at once.

## Security model - guarantees and honest limits

**Guarantees:** no dispatch runs without an accepted, in-scope, unexpired edge; each dispatch is cryptographically tied to a device of the named sender (a compromised broker cannot forge a sender or swap a pinned key); the agent is confined to the edge's tools, MCP grants, and path allowlist; MCP reachability is opt-in per server and sensitive tools are gated per call; revocation is immediate and cancels in-flight work; and user-intent approvals are resolved only on the recipient's own machine.

**Known limits (not papered over):** signing proves origin, not authorship - because composing happens in the browser, a fully-compromised broker could get a daemon to sign a task the user did not type (a trusted in-daemon compose surface would close this); TOFU pinning is vulnerable if the broker is compromised before a device's first contact; `Bash` grants full shell (no path checking); there is no end-to-end encryption - the broker relays plaintext instructions; and the in-memory replay-nonce set resets on daemon restart (bounded by the 5-minute freshness window).

## Partial and in-progress features

These exist in the codebase but are intentionally incomplete; listing them so the picture is honest.

- **SMS notifications (notify-only).** When a dispatch is routed to a recipient, the broker can text them so they know to open Dispatch - even if their daemon is offline. It's wired to Twilio: if `TWILIO_*` env vars are set, real texts go out; otherwise the call is a logged no-op so the flow still works in dev. It is best-effort (never blocks or fails a dispatch) and opt-in (the recipient saves a phone number; `sms_enabled` reflects whether the broker actually has Twilio configured). **Limitation:** it only *notifies* - you cannot reply to a text to approve. Approval still happens in a trusted UI.
- **Phone-as-approver (signed remote approval, in progress).** The goal: approve a dispatch or tool call from your phone while the task runs on your laptop - one tap, fully remote, without weakening the "broker can't fake intent" guarantee. The approach is a **signed relay**: a second enrolled device signs its decision with its own device key, the broker relays the opaque signed blob, and the running daemon verifies the signature against the approver device's enrolled public key before resolving the same approval it would have resolved locally. **Status:** the machine-to-machine core (canonical approval bytes, the broker `GET /devices/keys` roster + signed-decision relay endpoint, the daemon verify-and-resolve path, and a CLI signer standing in for the phone) is built and tested. **Not yet done:** notifying the approver device (so a phone actually buzzes), the phone PWA itself (WebCrypto key enrollment + approval inbox), and background push. Until the notify path lands, the doc statement that "the daemon ignores any approval frame over the broker socket" is being replaced by "the daemon *verifies* a signed approval frame" - the guarantee holds via signatures, not via refusing to listen.
- **Context sync - one-directional only.** `dispatch sync` (above) currently pulls a read-only activity digest on request. It is not yet a live, continuous, or two-way context-sharing channel between machines; richer shared context is a future direction.
- **Workflows - functional but maturing.** The workflow envelope, engine, node types, multi-recipient fan-out, and cron scheduling exist and run, but the graph authoring/UX and the breadth of node behaviors are still expanding.

## Deployment in brief

The broker needs Postgres and runs via uvicorn (locally with a Docker Postgres, or on Railway with the Postgres plugin and `DISPATCH_JWT_SECRET` set). The schema is idempotent and applied on every startup - no migration step. Optional integrations are env-gated: email (magic links), Twilio (SMS). Each user installs the daemon with the one-line installer the signed-in web page shows; after first enrollment a bare `dispatch-daemon` reconnects using saved config.
