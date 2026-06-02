---
name: dispatch
description: Peer-to-peer agentic task courier between two humans. Use when the user wants to delegate a task to a trusted contact's machine (their Claude agent runs it), check what's been dispatched to them, or accept/decline/track a dispatch. Triggers on natural-language patterns like "dispatch this to Edward", "send a task to Kaan's machine", "have Jeff's agent do X", "/dispatch", "what's in my dispatch inbox?", "accept that dispatch".
---

# Dispatch

Dispatch is a peer-to-peer courier for agentic work. One person describes a
task and names a recipient; the recipient's machine runs it with a local
Claude agent — but only across an explicit, scoped, revocable trust edge, the
task is cryptographically signed by the sender's device, and the recipient
approves it. The sender's verbatim task is preserved end to end.

## When to invoke

- **Send path** — the user says "dispatch this to <name>", "send a task to
  <name>'s machine", "have <name>'s agent do X". Resolve the recipient + the
  verbatim task, confirm, then run `dispatch send`.
- **Inbox path** — the user runs `/dispatch` or asks "what's been dispatched
  to me?". Run `dispatch inbox`, then offer to accept / decline / show each.
- **Accept / decline path** — after the user decides on an inbound dispatch,
  run `dispatch accept <id>` or `dispatch decline <id>`.
- **Track path** — "what's the status of that dispatch?" → `dispatch status
  <id>`; "show what I've sent" → `dispatch sent`.
- **Contacts path** — "who can I dispatch to?" → `dispatch contacts`.

## CLI

The `dispatch` CLI ships with the package (entry point `dispatch.cli:main`,
installed as the `dispatch` command alongside `dispatch-daemon`). Two surfaces:
**broker HTTP** for send/list/status, and the **local daemon's loopback API**
(`127.0.0.1`, token at `~/.dispatch/local.token`) for the decisions that must
be made on the recipient's own machine — accept/decline and per-tool approvals.
It reads the broker URL + JWT (and local port) the daemon already saved.

```
# Broker-backed (need broker creds; daemon online only where noted):
dispatch whoami                          # who am I + which broker
dispatch contacts                        # trust edges: who can dispatch to whom, scopes, online
dispatch send <recipient> '<task>'       # create a dispatch (your daemon must be ONLINE to sign)
  [--expires <seconds>]                  #   TTL, 60–86400 (default 3600)
  [--cwd <dir>]                          #   working-directory hint → metadata.cwd
  [--meta key=value]                     #   extra metadata (repeatable)
dispatch sent                            # dispatches I've sent + status
dispatch inbox                           # dispatches addressed to me + status
dispatch status <id>                     # one dispatch: status + full event trace
dispatch cancel <id>                     # cancel an in-flight dispatch (either party)

# Local-daemon-backed (resolved by THIS machine's dispatch-daemon, not the
# broker — the daemon ignores decisions relayed by the broker, by design):
dispatch accept <id>                     # accept an inbound dispatch → agent starts
dispatch decline <id>                    # decline an inbound dispatch
dispatch approvals                       # list tool calls awaiting allow/deny right now
dispatch approve <id> <request_id>       # allow ONE pending tool call (manual-edge gating)
dispatch deny <id> <request_id>          # deny ONE pending tool call
```

Add `--json` to any command for machine-readable output (works before or after
the subcommand). `--broker` / `--token` override the saved config. The
local-daemon commands need no broker creds — just a running `dispatch-daemon`.

The CLI resolves connection settings in this order:
- `--broker` / `--token` flags
- `$DISPATCH_BROKER` / `$DISPATCH_TOKEN`
- `~/.dispatch/config.json` (written by the daemon installer — the normal case)

`recipient` is the contact's **user id** (their email/identifier) exactly as
it appears under `peer` in `dispatch contacts`.

## Sender workflow

1. Run `dispatch contacts` if you're unsure of the exact recipient id or
   whether an outgoing edge exists. You can only send across an **outgoing**
   edge the recipient has accepted.
2. Confirm the recipient id and the verbatim task with the user (show both,
   ask Y/N) before sending. Do not paraphrase the task — Dispatch preserves it
   verbatim on purpose.
3. Run `dispatch send <recipient> '<task>' [--cwd <dir>] [--expires <s>]`.
   - Sending requires the **sender's own daemon to be online** — the broker
     asks it to sign (Layer 2). A `503` means your daemon is offline.
4. Report the returned `dispatch_id` so the user can track it
   (`dispatch status <id>`).

## Recipient workflow

1. Run `dispatch inbox`. For each pending dispatch, show the verbatim task and
   the sender, and ask the user: **accept**, **decline**, or **show details**
   (`dispatch status <id>`). `inbox`/`status` show **short** ids; the decision
   commands need the **full UUID** — get it from `dispatch inbox --json`.
2. If **accept**: run `dispatch accept <id>`. This goes to the recipient's
   **local daemon** (which must be running), not the broker. The agent then
   runs confined to the trust edge's tools and path allowlist.
3. If **decline**: run `dispatch decline <id>`.
4. **Accepting is NOT blanket approval.** When the edge is `approval: manual`,
   *every* tool call the agent makes pauses for a separate human allow/deny.
   Surface these and let the user decide each one — never auto-approve:
   - `dispatch approvals` — lists what's waiting (dispatch id, request id, tool,
     input).
   - `dispatch approve <id> <request_id>` / `dispatch deny <id> <request_id>`.
   Only an `approval: auto` edge runs tool calls without these prompts (still
   confined to the edge's tools + paths). The daemon's own local UI on
   `127.0.0.1:8001` does the same thing — the CLI is just a terminal front-end
   to it.
5. Track progress any time with `dispatch status <id>` (shows the live event
   trace: reasoning, tool calls, results).

## Trust boundary

Dispatch enforces trust in three independent layers — surface all three to the
user, never bypass them:

- **Layer 1 (broker):** a dispatch only goes out across an accepted, in-scope,
  unexpired trust edge, under the rate limit. No edge → `403`.
- **Layer 2 (recipient's machine):** the recipient's daemon verifies the
  sender's Ed25519 signature against a key pinned on first sight (TOFU). A
  swapped or stale key → rejected.
- **Layer 3 (the human):** for `approval: manual` edges, every tool call needs
  an explicit human allow/deny on the recipient's machine — via the daemon's
  local UI or `dispatch approvals` + `dispatch approve/deny`. The daemon
  resolves these locally and ignores any decision relayed by the broker, so a
  compromised broker can't fabricate approval.

The recipient (the human, via Claude) decides whether to accept — **never
auto-accept a dispatch**, even one that looks safe. The agent is confined to
the edge's tools and `paths` allowlist; granting `Bash` grants full shell, so
treat `Bash`-scoped edges with extra care.

## Failure modes

- `error: no token` — no `~/.dispatch/config.json` and no `--token`/env. Sign
  in to the broker and run the daemon installer, or pass `--token`.
- `broker error 403` on send — no accepted outgoing trust edge to that
  recipient. Invite them (web UI) and have them accept first.
- `broker error 503` on send — your own daemon is offline; it has to sign.
  Start `dispatch-daemon` and retry.
- `the local daemon isn't reachable on 127.0.0.1:…` on accept/approve — your
  `dispatch-daemon` isn't running on this machine. Start it and retry. (These
  commands hit the local daemon, never the broker.)
- `no local daemon token at …/local.token` — same cause: the daemon writes
  that token on start. Start `dispatch-daemon`.
- `nothing pending: …` on accept/approve — that dispatch/tool call has no open
  decision: already decided, expired, or the daemon hasn't surfaced it yet.
  Re-check `dispatch inbox` / `dispatch approvals`.
- `broker error 401` — token expired/invalid (or the broker has a different
  `DISPATCH_JWT_SECRET` than issued it). Re-authenticate.
- `can't reach broker` — wrong `--broker`/`$DISPATCH_BROKER`, or the broker
  isn't running. Check `dispatch whoami`.
- `404` on accept/approve — the local daemon doesn't know that id; use the full
  UUID from `dispatch inbox --json`, and confirm the daemon was up when the
  dispatch arrived.
