# Design: Phone-as-Approver (remote signed approval)

Status: **steps 1-2 implemented** · Author: ewar · Trust model: **signed relay (zero-trust broker)**

> **Build status.** Steps 1-2 (the machine-to-machine proof) are done and tested:
> a second *machine* can sign a tool decision and have the runner verify + act on
> it, with no phone/WebCrypto yet. Remaining: 3 (broker fan-out of
> `approval_request` so the approver is *notified*), 4 (PWA WebCrypto enroll +
> approval inbox), 5 (Web Push). See "Implemented so far" at the bottom.

## Goal

Let the **"yes" happen on a different device than the one running the task.**
Kaan sends me a dispatch. My laptop daemon accepts and starts executing. A tool
call needs approval. I'm away from the laptop - so the prompt fans out to my
**phone**, I tap **Yes**, and the laptop keeps running. Frictionless, but there
is still a deliberate human tap, so it never feels like it's running unattended.

The phone is *not* a runner. It never executes anything. It is purely a second
**approval surface** for a task executing elsewhere.

## The one hard constraint we must not break

Today approvals are resolved **only** on the running daemon's `127.0.0.1` HTTP
endpoint (`daemon/local_app.py`). The broker is shown the `permission_request`
for *display* but is structurally incapable of *answering* it. That is the
security model: **a compromised broker cannot fabricate your intent.**

A naive "broker forwards the user's yes" would hand the broker exactly that
power. So we keep the guarantee by having the **phone sign its decision** with a
device keypair the broker never holds. The broker becomes a dumb relay of an
opaque signed blob; the **running daemon verifies** the signature against the
phone's enrolled public key before resolving. Broker compromise → at worst it
can *drop* or *delay* an approval, never *forge* one. Same property the dispatch
layer already has.

## What already exists that we reuse (no new crypto)

The codebase already does this exact round-trip - just in the other direction.

| Need | Existing thing | Location |
| --- | --- | --- |
| Ed25519 sign/verify/b64 | `crypto.sign / verify / b64encode / PUBLIC_KEY_BYTES` | `shared/crypto.py` |
| Deterministic signing bytes | `canonical_dispatch_bytes(...)` pattern | `shared/signing.py` |
| Device = keypair + enrolled pubkey | `devices` table, `public_key BYTEA` | `broker/schema.sql:70` |
| Broker→device "do a crypto thing, return it over WS" | `_request_signature()` + `_pending_signatures[request_id]` Future | `broker/app.py:507` |
| Device returns signed result over WS | `_handle_agent_message` resolves the Future | `broker/app.py:1270` |
| Daemon side: pending approval Future keyed by `(dispatch_id, request_id)` | `state.pending_approvals` | `daemon/main.py:198,1040` |
| Multi-device fan-out target | `STATE.agents[user_id][device_id] = ws` | `broker/state.py` |
| Get a device's pubkey to verify | `STORE.get_device_public_key(device_id)` | `broker/store.py:213` |
| Persist "always allow" back to the edge | `_persist_always_tool()` | `daemon/main.py:1175` |

We are mirroring `sign_request` (broker asks the *sender's* daemon to sign a
dispatch) into an **`approval_request`** (broker asks the *recipient's phone* to
sign a decision). Symmetry means low risk.

## Canonical bytes the phone signs

Add to `shared/signing.py`:

```python
def canonical_approval_bytes(
    *,
    dispatch_id: str,
    request_id: str,     # the per-tool-call id from the running daemon
    tool_name: str,      # exact name, e.g. "Bash" / "mcp__notion__notion-move-pages"
    decision: str,       # allow | deny | always | session
    approver_device: str,# the phone's device_id - binds sig to this device
    issued_at: str,      # ISO-8601, replay window
) -> bytes:
    obj = {
        "dispatch_id": dispatch_id,
        "request_id": request_id,
        "tool_name": tool_name,
        "decision": decision,
        "approver_device": approver_device,
        "issued_at": issued_at,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

Binding `tool_name` + `request_id` means a captured signature can't be replayed
onto a *different* tool call, and `approver_device` + `issued_at` pin who/when.

## End-to-end flow

```
laptop daemon (runner)            broker (relay)                 phone (approver PWA)
─────────────────────             ──────────────                ──────────────────
can_use_tool() hits a gate
 opens Future
   pending_approvals[(d,r)]
 emits permission_request ───────▶ stored + (NEW) fan-out  ─────▶ push to every OTHER
   (as today, for display)         to user's other online        online device of the
                                    devices as approval_request    same user
                                                                  shows card: tool+input
                                                                  user taps "Always"
                                                                  WebCrypto signs
                                                                  canonical_approval_bytes
                                   resolve relay  ◀───────────────  {decision, signature,
                                   verify approver owns device      approver_device}
        (NEW) decision  ◀───────── relay signed blob to the
        + signature over runner's   runner daemon's WS
        existing WS
 verify(pubkey, bytes, sig)
   pubkey from new_dispatch? no -    daemon fetches/caches my own
   devices' pubkeys (see below)
 if valid: resolve Future with
   decision  → exact same path as
   a local 127.0.0.1 decision
 "always" → _persist_always_tool()
```

Local approval (`127.0.0.1`) keeps working unchanged and races the phone: first
valid decision wins, the Future resolves once, the other surface no-ops. So the
laptop UI and the phone are just two doors to the same lock.

## Where the runner gets the phone's public key

The runner must verify a signature from *another of my own devices*. Two options:

- **A. Daemon pulls its own device roster.** New broker route
  `GET /devices/keys` → `[{device_id, public_key_b64, status}]` for the
  authenticated user. Daemon caches it, refreshes on a verify miss. Clean,
  daemon-verifies (preserves zero-trust). **Recommended.**
- **B. Broker attaches `approver_public_key` when relaying.** Simpler but the
  daemon would be trusting the broker to hand it the right key - weakens the
  guarantee back toward "trust the broker." Reject.

Go with **A**.

## Components to build

### 1. `shared/signing.py`
- `canonical_approval_bytes(...)` (above).

### 2. Broker (`broker/app.py`, `store.py`, `schema.sql`)
- **Fan-out on permission_request.** Today the runner emits `permission_request`
  as a dispatch *event*. Add: when the broker sees a pending tool approval for a
  dispatch, push an `approval_request` frame to **every other online device** of
  the recipient (not the runner). Carries `{dispatch_id, request_id, tool_name,
  tool_input, runner_device}`. (Mirror of `_request_signature`'s send.)
- **Relay endpoint / WS message `approval_decision`.** Phone → broker delivers
  `{dispatch_id, request_id, decision, approver_device, issued_at, signature}`.
  Broker checks the approver_device belongs to this user + is `active`, then
  relays the blob verbatim to the **runner_device's** WS. Broker does **not**
  interpret the decision.
- `GET /devices/keys` (option A).
- Optional: a `pending_tool_approvals` table only if we want approvals to
  survive a broker restart / reach a phone that was offline at request time.
  v1 can keep it in-memory (matches today's `_pending_signatures`).

### 3. Daemon (`daemon/main.py`)
- On the runner's WS message loop, handle a new `approval_decision` frame:
  rebuild `canonical_approval_bytes`, `crypto.verify` against the approver
  device's cached pubkey (option A), and if valid resolve
  `state.pending_approvals[(dispatch_id, request_id)]` with the decision -   the **same** Future the local endpoint resolves. Reuse the existing
  `always`/`session`/`allow`/`deny` handling verbatim, including
  `_persist_always_tool()`.
- Device-key cache + `GET /devices/keys` client.
- Reject: unknown device, revoked device, `issued_at` outside a small window
  (e.g. ±120 s, matching `TOOL_APPROVAL_TIMEOUT_S`), signature mismatch.

### 4. Phone surface - the existing PWA in **broker mode**
No native app. The desktop SPA already runs against the broker (`isBroker`),
and today `decideTool` / `decide` call `redirectToLocal()` to *refuse* remote
approval. We flip that for devices that hold a key:
- **Enroll the phone as a device from the browser.** Generate an Ed25519 keypair
  with **WebCrypto**, store the private key in **IndexedDB (non-extractable
  where possible)**, POST the public key to the existing `/devices/enroll`. The
  phone now shows up in the Devices list like any machine (label "iPhone").
- **Live approval inbox.** Subscribe to `approval_request` pushes (the broker
  web socket / SSE the SPA already uses for inbox updates). Render the
  tool + input with Yes / No / Always / This-session.
- **Sign on tap.** Build the same canonical bytes, sign with the IndexedDB key,
  POST `approval_decision` to the broker. Replace `redirectToLocal()` for the
  decision path *when a local signing key exists*; keep the redirect fallback
  for a browser with no enrolled key.

### 5. "Fully autonomous" still wins first
If the edge has `approval: "auto"` or the tool is in `auto_tools`, the runner
**never opens the Future** - so no fan-out, no phone buzz. Phone approval only
engages for tools that would have prompted anyway. This is the "give this guy
access to everything, no manual permissions" path you already shipped; it's
strictly upstream of all of the above.

## Security notes

- **Zero-trust broker preserved.** Broker can drop/delay/reorder, never forge. A
  decision without a valid signature from an enrolled, active device of the
  *recipient* is discarded by the runner.
- **Replay-bound.** Signature covers `request_id` + `tool_name` + `issued_at`;
  a sig can't be lifted onto another call or re-fired after the window.
- **Revocation is immediate.** Verify pulls from the live roster; a revoked
  phone's signatures fail. (Already: revoke drops the WS, `app.py:926`.)
- **Private key never leaves the phone.** WebCrypto non-extractable; broker only
  ever holds the public key, exactly like every other device.
- **Decision races are safe.** Future resolves once; whichever door (laptop or
  phone) answers first wins, the rest no-op.
- **Phone offline at request time.** v1: it simply misses it; laptop UI + the
  120 s timeout still govern. v2: persist pending approvals so a phone coming
  online can still answer within the window.

## Open questions

1. **Push when the PWA is backgrounded.** WebSocket/SSE only fires with the tab
   alive. Real "buzz my phone" needs Web Push (VAPID) or piggybacks the existing
   Twilio SMS as the *nudge* ("open Dispatch to approve") while the actual
   signed yes still happens in the PWA. SMS-reply-to-approve was explicitly
   *not* chosen (coarse, broker-trusting), so SMS stays a nudge only.
2. **Accept/Reject of the whole dispatch from phone**, not just per-tool. Same
   mechanism (sign a `decision` over the dispatch_id); worth folding in.
3. **Per-device approval policy** - e.g. "phone may say `session` but not
   `always`." Probably a later refinement; v1 lets any enrolled device give any
   decision.
4. **In-memory vs persisted pending approvals** (drives whether a just-woke
   phone can still answer). Recommend in-memory for v1 to match `_pending_signatures`.

## Suggested build order

1. `canonical_approval_bytes` + unit tests (pure, no infra).
2. `GET /devices/keys` + daemon key cache + daemon `approval_decision` verify →
   resolve Future. Test with a CLI that signs with an enrolled *machine* key
   (proves the runner path before any phone/WebCrypto exists).
3. Broker fan-out of `approval_request` to other online devices + relay of
   `approval_decision` to the runner.
4. PWA: WebCrypto enroll + IndexedDB key + approval inbox + sign-on-tap.
5. Web Push (or SMS-nudge) so a backgrounded phone actually rings.

Steps 1–2 are testable end-to-end with zero phone work and de-risk the whole
feature: if a second *machine* can remotely approve a task running on the first,
the phone is just a nicer client for the same proven path.

## Implemented so far (steps 1-2)

- **`shared/signing.py`** - `canonical_approval_bytes(dispatch_id, request_id,
  tool_name, decision, approver_device, issued_at)`. Tests in
  `tests/test_signing_approval.py` (determinism, field-binding, sign/verify,
  replay-onto-another-call protection).
- **Broker** (`broker/app.py`, `store.py`):
  - `GET /devices/keys` → the user's active devices + public keys.
  - `POST /dispatch/{id}/tool/{req}/remote-decision` - authorizes (caller is the
    dispatch recipient; `approver_device` is their active device), then relays an
    opaque signed `approval_decision` frame to the recipient's online devices.
    Never interprets or resolves the decision.
  - `store.list_device_keys`, `store.device_belongs_to`.
- **Daemon** (`daemon/main.py`):
  - `DaemonState.device_keys` roster cache, warmed on connect via
    `_refresh_device_keys`, refreshed on a key-miss.
  - `approval_decision` WS frame → `_resolve_remote_approval`: rebuilds the
    canonical bytes (tool name pulled from the live pending-tool record),
    `crypto.verify` against the approver's key, freshness check
    (`REMOTE_APPROVAL_MAX_AGE_S = 300`), then resolves the *same*
    `pending_approvals` Future the local 127.0.0.1 endpoint resolves. Local and
    remote race; first valid decision wins. `always`/`session` reuse the existing
    grant + `_persist_always_tool` path unchanged.
- **CLI** (`cli.py`): `dispatch approve-remote <dispatch_id> <request_id>
  <tool_name> [--always|--session|--deny]` - the terminal stand-in for the phone:
  signs with this machine's enrolled device key and POSTs the remote-decision.
- **Tests**: `tests/test_remote_approval.py` drives the runner verify/resolve
  path directly (valid resolves; bad-sig / wrong-key / tampered-tool / stale /
  unknown-device all refuse to resolve; no-Future and missing-roster no-op).
  Full suite: 32 passed.

**Not yet wired:** the approver is not *notified* - step 3 (broker fan-out of an
`approval_request` to other devices) is what makes a phone buzz. Until then the
remote approver must already know the `request_id` (e.g. from the runner's
`dispatch approvals` / local UI). That's expected for the proof.
