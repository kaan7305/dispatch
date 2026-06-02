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
installed as the `dispatch` command alongside `dispatch-daemon`). It talks to
the broker's HTTP API and, for accept/decline, the `/inbox` WebSocket. It
reads the broker URL + JWT the daemon already saved.

```
dispatch whoami                          # who am I + which broker
dispatch contacts                        # trust edges: who can dispatch to whom, scopes, online
dispatch send <recipient> '<task>'       # create a dispatch (your daemon must be ONLINE to sign)
  [--expires <seconds>]                  #   TTL, 60–86400 (default 3600)
  [--cwd <dir>]                          #   working-directory hint → metadata.cwd
  [--meta key=value]                     #   extra metadata (repeatable)
dispatch sent                            # dispatches I've sent + status
dispatch inbox                           # dispatches addressed to me + status
dispatch status <id>                     # one dispatch: status + full event trace
dispatch accept <id>                     # accept an inbound dispatch (my daemon must be ONLINE)
dispatch decline <id>                    # decline an inbound dispatch
dispatch cancel <id>                     # cancel an in-flight dispatch (either party)
```

Add `--json` to any command for machine-readable output (what you parse).
`--broker` / `--token` override the saved config.

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
   (`dispatch status <id>`).
2. If **accept**: run `dispatch accept <id>`. The recipient's daemon must be
   **online** for the decision to take effect; the agent then runs confined to
   the trust edge's tools and path allowlist.
   - **Tool-call approvals (Layer 3) are not driven from this CLI.** When the
     edge is `approval: manual`, each destructive tool call prompts the human
     in the daemon's local approval UI / web inbox. Direct the user there;
     never try to auto-approve on their behalf.
3. If **decline**: run `dispatch decline <id>`.
4. Track progress any time with `dispatch status <id>` (shows the live event
   trace: reasoning, tool calls, results).

## Trust boundary

Dispatch enforces trust in three independent layers — surface all three to the
user, never bypass them:

- **Layer 1 (broker):** a dispatch only goes out across an accepted, in-scope,
  unexpired trust edge, under the rate limit. No edge → `403`.
- **Layer 2 (recipient's machine):** the recipient's daemon verifies the
  sender's Ed25519 signature against a key pinned on first sight (TOFU). A
  swapped or stale key → rejected.
- **Layer 3 (the human):** for `approval: manual` edges, every destructive
  tool call needs an explicit human approval in the daemon UI.

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
- `accept`/`decline` sent but "no echo" — the recipient's daemon is offline,
  so the broker dropped the decision. Start `dispatch-daemon` and re-run.
- `broker error 401` — token expired/invalid (or the broker has a different
  `DISPATCH_JWT_SECRET` than issued it). Re-authenticate.
- `can't reach broker` — wrong `--broker`/`$DISPATCH_BROKER`, or the broker
  isn't running. Check `dispatch whoami`.
- `dispatch <id> is not in your inbox` on accept — wrong/short id; run
  `dispatch inbox` and use the full UUID.
