# Design: Scheduled Dispatches

Status: **proposed** · Author: ewar · Trust model: **unchanged (sender-local schedules, signed-at-fire-time)**

## Goal

Let a sender say "every weekday at 9am, dispatch *summarize overnight CI
failures* to Kaan" - or "send this to Jeff tomorrow at 5pm" - and have the
sender's daemon fire it on time, with zero changes to what the recipient
sees or controls. A scheduled occurrence is an **ordinary dispatch** in
every way that matters: trust-checked, signed at fire time, replay-guarded,
counted against the daily limit, approved tool-by-tool under the
recipient's policy. The schedule itself is sender-machine state, like a
crontab entry.

Two shapes, one feature:

- **Recurring**: a cron expression (`0 9 * * 1-5`), fires until paused,
  expired, or capped.
- **One-shot**: a single future timestamp (`--at`), fires once and
  self-deletes.

## The constraints we must not break

1. **The broker never gains authority.** Today the broker relays, persists,
   and notifies; it holds no keys and decides no permissions. A schedule
   stored broker-side would make the broker the thing that *originates*
   tasks on a cadence - a compromised broker could re-time, replay, or
   multiply them. So schedules live on the **sender's machine** and the
   broker only ever sees the dispatches they produce, signed fresh each
   time. To the broker, a scheduled send is indistinguishable from the
   human typing `dispatch send` at that moment.

2. **Each occurrence is one-time-use.** Every fire goes through
   `POST /dispatch` → broker trust check → sign-request back to our own
   daemon over the broker WS → fresh `dispatch_id`, fresh signature, fresh
   nonce. No pre-signed envelopes, no nonce bookkeeping, no interaction
   with the replay guard's retention horizon.

3. **The recipient's protections apply unmodified.** Occurrences count
   against `max_dispatches_per_day` (same precedent as sync). Manual-approval
   edges still require a human tap per tool call. The schedule metadata we
   attach is informational for the inbox UI only - the recipient daemon
   must derive **zero policy** from it.

4. **"Your daemon must be online to sign" extends honestly to "your daemon
   must be online to fire."** If the sender's laptop is asleep at 9am,
   nothing fires - and the catch-up policy decides what happens on wake.
   We do not pretend to be a cloud cron.

## What already exists that we reuse

| Need | Existing thing | Location |
| --- | --- | --- |
| Cron evaluation, 60s tick loop | `CronScheduler` (workflow trigger.cron) | `daemon/workflow_scheduler.py` |
| `croniter` dependency | already imported/guarded there | same |
| Fire path = the normal send path | broker `POST /dispatch` + `_request_signature` round-trip | `broker/app.py` |
| Sender-local persisted state dir | `dispatch_home()` (`~/.dispatch/`) | `daemon/identity.py:28` |
| Single-owner gating | `ConnectionLock` | `daemon/connlock.py` |
| Broker URL + rotating token access | `broker_token_getter` callable pattern | `workflow_scheduler.py:35` |
| Per-occurrence TTL ceiling vs nonce horizon | `expires_in_seconds` validator (≤ 30d ≤ `OFFLINE_RETENTION_S`) | `schema.py:129` |
| Inbox metadata pass-through | `DispatchPayload.metadata` / `public_metadata` | `schema.py:95` |

The workflow cron scheduler proves the shape (daemon-local tick → fire via
broker as if a human clicked). We deliberately do **not** reuse two of its
behaviors:

- **Restart re-fire.** Its `last_fired` is in-memory, cold-anchored to year
  2000, so the first tick after any daemon restart fires every cron once.
  Dispatch schedules persist `last_fired_at` to disk; restarts are silent.
- **Standby double-tick.** It is started before the connection lock is
  acquired, so a standby process ticks too. The dispatch scheduler ticks
  **only while this process owns the broker connection**.

(Both are worth back-porting to the workflow scheduler; tracked as a
follow-up, not part of this build.)

## The schedule record

New model in `shared/schema.py` (local-only - it never crosses the wire,
but living next to the wire types keeps validation in one place):

```python
class DispatchSchedule(BaseModel):
    schedule_id: UUID = Field(default_factory=uuid4)
    recipient_id: str                    # one recipient per schedule;
                                         # fan-out = N schedules (independent
                                         # pause/failure state per edge)
    task: str                            # same TASK_MAX_CHARS bound as a send
    cron: Optional[str] = None           # recurring (croniter syntax)
    at: Optional[datetime] = None        # one-shot; exactly one of cron/at
    timezone: str = "local"              # IANA name; cron evaluated in this tz
                                         # (DST-correct "9am" means 9am)
    expires_in_seconds: Optional[int] = None
                                         # per-occurrence TTL; default =
                                         # min(86400, cron interval) so a daily
                                         # task never has two live copies
    metadata: dict[str, Any] = Field(default_factory=dict)

    # behavior knobs
    enabled: bool = True
    skip_if_prior_active: bool = True    # don't fire while the previous
                                         # occurrence is pending/accepted/running
    catch_up: Literal["skip", "fire_once"] = "fire_once"
                                         # what to do with fires missed while
                                         # the daemon was down/asleep
    until: Optional[datetime] = None     # recurring end date
    max_occurrences: Optional[int] = None

    # bookkeeping (persisted, never user-set)
    occurrence_count: int = 0
    last_fired_at: Optional[datetime] = None
    last_dispatch_id: Optional[UUID] = None
    last_result: Optional[str] = None    # "ok" | short error string
    consecutive_failures: int = 0        # auto-pause at FAILURE_PAUSE_THRESHOLD
    created_at: datetime = Field(default_factory=utcnow)
```

Storage: `~/.dispatch/schedules.json` - a versioned
`{"version": 1, "schedules": [...]}` file, written atomically
(tmp + rename) on every mutation, loaded at daemon start. Volume is
human-scale (a handful per user); JSON keeps it inspectable and
hand-fixable, consistent with `config.json` and `shareable-mcp.json`.

## The scheduler (new `daemon/dispatch_scheduler.py`)

A sibling of `CronScheduler`, not an extension of it - the workflow one
polls broker-side definitions, this one owns local state. ~150 lines.

Tick loop, every 30s, gated on connection ownership:

```
for each enabled schedule:
    due = next_fire(cron, last_fired_at, tz) <= now      # or `at` <= now
    if not due: continue
    if skip_if_prior_active and last occurrence still pending/accepted/running:
        record "skipped: prior active"; advance last_fired_at; continue
    fire:
        POST {broker}/dispatch  {recipient_id, task,
                                 expires_in_seconds,
                                 metadata + schedule stamp}
    on success:  last_fired_at = now; occurrence_count += 1;
                 consecutive_failures = 0; persist
    on failure:  last_result = error; consecutive_failures += 1; persist
                 if consecutive_failures >= 3: enabled = False; notify tray
    if one-shot, or until/max_occurrences reached: delete/disable; persist
```

Details that matter:

- **Catch-up on wake/restart.** Because `last_fired_at` is persisted, the
  first tick after downtime sees exactly how many fires were missed.
  `fire_once` (default): collapse all missed occurrences into a single
  fire now - "I still want this morning's digest even though I opened the
  laptop at 9:40." `skip`: advance to the next future occurrence silently.
  Either way, **never** fire N times for N missed slots.
- **`skip_if_prior_active` checks the previous occurrence's status** via
  the daemon's own local state (it already mirrors broker dispatch status
  for the sent-items view). A daily "triage the queue" task that nobody
  accepted yesterday should not stack a second copy on top - the short
  default TTL (≤ one interval) makes the old one expire right as the new
  one would fire, and this knob covers the accepted-but-still-running case.
- **Failure taxonomy.** "Broker unreachable" and "daemon offline" are not
  failures - the schedule simply isn't evaluated, and catch-up handles it.
  Failures are *rejections*: trust edge revoked, daily limit hit, recipient
  unknown, task validation. Three consecutive rejections auto-pause the
  schedule and surface a tray notification, so a revoked edge doesn't turn
  into a silent daily error loop.
- **Ownership gating.** The tick body returns immediately unless this
  process holds the `ConnectionLock`. On failover the new owner loads
  `schedules.json` and continues from the persisted `last_fired_at` -
  no double-fire, no gap.

## What the recipient sees

Each fired occurrence carries a stamp in `metadata`:

```json
"schedule": {
  "schedule_id": "…",
  "occurrence": 14,
  "cron": "0 9 * * 1-5"
}
```

The inbox can badge it ("↻ recurring · weekdays 9am · #14 from Edward")
and, later, offer "mute this schedule" (v2: recipient-side auto-decline
keyed on `schedule_id`; requires nothing from the sender). The recipient
daemon treats the stamp as opaque display data: `can_use_tool`, path
gating, approval flow, and limits are completely unaware of it.

## Surface

**CLI** - scheduling is a modifier on `send`, management is a new verb:

```
dispatch send --to kaan --every "0 9 * * 1-5" "summarize overnight CI failures"
dispatch send --to jeff --at "2026-06-13T17:00" "rotate the staging certs"

dispatch schedule list
dispatch schedule pause|resume|rm <id-prefix>
dispatch schedule run-now <id-prefix>      # fire immediately, don't move the clock
```

`--every`/`--at` on `send` validate the cron/timestamp, write the schedule
through the daemon's local API, and print the schedule id + next fire time.
They do **not** send anything immediately.

**Daemon local API** (`local_app.py`) - the CLI and tray UI go through the
daemon (it owns the file and the running scheduler's view of it):

```
GET    /api/schedules
POST   /api/schedules
PATCH  /api/schedules/{id}      # enabled, cron, task, knobs
DELETE /api/schedules/{id}
POST   /api/schedules/{id}/fire
```

**MCP** - no sixth tool; the grouped-tool invariant holds:

- `dispatch_send` grows `every` / `at` (mutually exclusive, both optional).
- `dispatch_read` grows `schedules` as a readable collection.
- `dispatch_act` grows `pause_schedule` / `resume_schedule` /
  `cancel_schedule` / `fire_schedule` actions.

## Edge cases

- **DST.** Cron is evaluated in the schedule's IANA timezone with a
  localized base, so "9am" stays 9am across transitions. The nonexistent
  hour on spring-forward resolves to the croniter default (next valid
  instant); the repeated hour on fall-back fires once (guarded by
  `last_fired_at`).
- **Clock skew / sleep mid-tick.** The fire decision uses
  `next_fire(last_fired_at) <= now`, never equality with a slot, so a tick
  that lands late by minutes still fires (subject to catch-up policy).
- **TTL vs cadence.** Default per-occurrence TTL is
  `min(86400, interval)`. A user can raise it explicitly (bounded by the
  existing ≤ 30d / nonce-horizon ceiling), accepting that copies may
  coexist if the recipient is long-offline; `skip_if_prior_active` then
  becomes the real backstop.
- **Edge deleted while schedule exists.** First fire after revocation is a
  rejection → counts toward auto-pause → paused with reason after three.
  `dispatch schedule list` shows the last error inline.
- **Two machines, one user.** Identity is per-machine, so schedules are
  too - a schedule created on the laptop fires from the laptop. This is a
  feature (the signing key and the schedule live together), but the CLI
  should say so when listing: `schedules on this machine`.

## Rejected alternatives

- **Broker-stored schedules.** Would survive sender-machine reinstalls and
  be visible from any device - but the broker still cannot sign, so the
  sender daemon must be online at fire time *anyway*. All cost (new broker
  tables/endpoints, broker becomes a task originator, new attack surface
  on re-timing/multiplying tasks), no availability win. The workflow cron
  already pays this cost because workflow *definitions* legitimately live
  broker-side; plain dispatches have no such anchor.
- **Pre-signed envelopes** (sign N future occurrences in advance, broker
  fires them while the sender sleeps). The only option with a real
  availability win, and the only one with real teeth: pre-signed envelopes
  are bearer artifacts sitting on the broker, each needs a pre-allocated
  nonce and a validity window wide enough to be useful but narrow enough
  to be safe, and cancellation requires a revocation channel that works
  *while you're offline* - which is the exact situation that motivated
  them. Defer until "fire while my laptop is closed" is a demonstrated
  need; if it comes, scope it to `auto`-approval edges only is **wrong**
  (those are the most dangerous) - it would need its own design doc.
- **A schedule as a one-node workflow.** Tempting reuse, but it drags in
  the broker-side definition storage, the run-history machinery, and the
  workflow scheduler's known restart/standby issues for what is
  semantically a plain send with a timer. Workflows can keep their cron
  for DAGs; single tasks deserve the lighter path.

## Build plan

1. **Schema + store** - `DispatchSchedule` in `shared/schema.py`;
   `daemon/schedule_store.py` (load/save/mutate `schedules.json`,
   atomic writes). Pure-unit testable.
2. **Scheduler** - `daemon/dispatch_scheduler.py`: tick loop, ownership
   gate, catch-up, failure/auto-pause. Tests with a fake clock and a
   stubbed broker client (`test_dispatch_scheduler.py`).
3. **Local API + CLI** - routes in `local_app.py`; `dispatch schedule …`
   verb + `--every/--at` on `send`.
4. **MCP + inbox badge** - grouped-tool extensions; recipient inbox renders
   the `schedule` metadata stamp.
5. **Follow-up (separate change)** - back-port persisted `last_fired` and
   ownership gating to `workflow_scheduler.py`.
