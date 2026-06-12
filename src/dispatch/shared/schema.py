"""Shared types for Dispatch.

DispatchPayload is the contract between sender, broker, and recipient.
DispatchEvent is the per-step event shape forwarded by every component.

The two principles:
  - Every field a caller might need lives on the payload (typed).
  - Wire format is always JSON. Pydantic handles validation in / out.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Literal, Optional, TypedDict
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

VALID_TOOLS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch")

# Rich-payload caps. The task and any attachments travel inside the dispatch
# envelope (broker DB row + one WS frame), so these bound a single dispatch's
# wire size. Attachment bytes are base64 in metadata; the *manifest*
# (name/size/sha256) is covered by the Ed25519 signature — see signing.py.
TASK_MAX_CHARS = 100_000
# A human chat message pinned to a dispatch thread (display-only — never reaches
# the executor). Generous, but bounded so one message can't blow the event row.
MESSAGE_MAX_CHARS = 8_000
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024           # one file
ATTACHMENTS_MAX_TOTAL_BYTES = 250 * 1024 * 1024  # whole dispatch (50 × 5 MB)
ATTACHMENTS_MAX_COUNT = 50
ATTACHMENT_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DispatchStatus(str, enum.Enum):
    awaiting_signature = "awaiting_signature"  # sender's daemon was offline; broker holds it unsigned until it reconnects
    pending = "pending"        # broker has it, recipient daemon offline
    delivered = "delivered"    # daemon pulled it from broker
    accepted = "accepted"      # recipient pressed Accept
    running = "running"        # agent session active
    completed = "completed"    # agent finished cleanly
    denied = "denied"          # recipient pressed Reject (top-level)
    failed = "failed"          # exception during execution
    expired = "expired"        # past expires_at without acceptance
    cancelled = "cancelled"    # trust edge revoked while in-flight


class DispatchAttachment(BaseModel):
    """One file riding on `DispatchPayload.metadata["attachments"]`.

    The bytes travel base64 in `content_b64`; integrity comes from the
    manifest entry (name/size/sha256) being bound into the dispatch
    signature, and the recipient daemon re-hashing the decoded bytes
    against `sha256` before the file ever touches disk.
    """

    name: str = Field(..., pattern=ATTACHMENT_NAME_RE)
    content_b64: str = Field(..., min_length=1)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    size: int = Field(..., ge=1, le=ATTACHMENT_MAX_BYTES)


class DispatchContext(BaseModel):
    """Structured sender context riding on `metadata["context"]`.

    All fields optional; empties are dropped from the canonical (signed)
    form. `background` is free prose — typically the sending agent's
    summary of its own session, so the recipient agent doesn't start cold.
    """

    project: str = Field(default="", max_length=200)
    links: list[str] = Field(default_factory=list, max_length=10)
    deliverable: str = Field(default="", max_length=2000)
    background: str = Field(default="", max_length=20_000)

    @field_validator("links")
    @classmethod
    def _link_lengths(cls, v: list[str]) -> list[str]:
        bad = [l for l in v if len(l) > 500]
        if bad:
            raise ValueError("links must each be <= 500 chars")
        return v


class DispatchPayload(BaseModel):
    """The signed-over, cross-party dispatch envelope."""

    dispatch_id: UUID = Field(default_factory=uuid4)
    sender_id: str = Field(..., min_length=1, max_length=64)
    recipient_id: str = Field(..., min_length=1, max_length=64)
    task: str = Field(..., min_length=1, max_length=TASK_MAX_CHARS)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


def public_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata as exposed in list/detail API responses: attachment *bytes*
    are dropped (a detail fetch shouldn't drag megabytes of base64), keeping
    only the manifest fields the UI renders. Everything else passes through."""
    md = dict(metadata or {})
    atts = md.get("attachments")
    if isinstance(atts, list):
        md["attachments"] = [
            {k: a.get(k) for k in ("name", "size", "sha256")}
            for a in atts
            if isinstance(a, dict)
        ]
    return md


class DispatchCreateRequest(BaseModel):
    """Sender → broker body for POST /dispatch.

    Provide either `recipient_id` (single) or `recipient_ids` (fan-out).
    The broker runs the trust check + signing flow for each recipient
    independently and returns one result per recipient.
    """

    recipient_id: Optional[str] = Field(default=None, max_length=64)
    recipient_ids: Optional[list[str]] = Field(default=None, max_length=50)
    task: str = Field(..., min_length=1, max_length=TASK_MAX_CHARS)
    # Async/offline delivery: a dispatch may wait for a long-offline recipient,
    # so the lifetime defaults to a day and may run up to 30 days. The ceiling
    # MUST stay <= the recipient daemon's nonce-retention horizon
    # (daemon.main.OFFLINE_RETENTION_S), or a dispatch could outlive the
    # replay-guard record that keeps it one-time-use.
    expires_in_seconds: int = Field(default=86400, ge=60, le=30 * 24 * 3600)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_exactly_one_recipient_form(self) -> "DispatchCreateRequest":
        single = bool(self.recipient_id and self.recipient_id.strip())
        many = bool(self.recipient_ids)
        if not (single or many):
            raise ValueError("recipient_id or recipient_ids is required")
        if single and many:
            raise ValueError("provide recipient_id OR recipient_ids, not both")
        return self

    def normalized_recipients(self) -> list[str]:
        """Always-returns-a-list helper for downstream fan-out logic."""
        if self.recipient_ids:
            seen: set[str] = set()
            out: list[str] = []
            for r in self.recipient_ids:
                r = r.strip()
                if r and r not in seen:
                    seen.add(r)
                    out.append(r)
            return out
        assert self.recipient_id  # validator guarantees it
        return [self.recipient_id.strip()]


class LoginRequest(BaseModel):
    """Dev-mode login. Kept for CLI and tests; not exposed in the UI."""

    username: str = Field(..., min_length=1, max_length=128)


class ClerkExchangeRequest(BaseModel):
    """Body for POST /auth/clerk. The SPA sends the Clerk session JWT and
    gets back a broker-issued Dispatch JWT in exchange."""

    clerk_token: str = Field(..., min_length=10)


class PhoneUpdateRequest(BaseModel):
    """Body for POST /me/phone. Sets the recipient's SMS notification number,
    or clears it when `phone` is null/empty. Stored and sent in E.164 form."""

    phone: Optional[str] = Field(default=None, max_length=20)

    @field_validator("phone")
    @classmethod
    def _validate_e164(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().replace(" ", "").replace("-", "")
        if v == "":
            return None
        # E.164: a leading + and 8–15 digits (country code included).
        digits = v[1:] if v.startswith("+") else v
        if not v.startswith("+") or not digits.isdigit() or not (8 <= len(digits) <= 15):
            raise ValueError("phone must be E.164, e.g. +14155550123")
        return v


class DeviceEnrollRequest(BaseModel):
    """Daemon → broker body for POST /devices/enroll."""

    label: str = Field(..., min_length=1, max_length=128)
    public_key: str = Field(..., description="Ed25519 public key, base64-encoded")


class SyncScope(BaseModel):
    """Standing permission for a sender to pull READ-ONLY *activity digests* of
    this machine's Claude Code sessions (the `dispatch sync` feature).

    Deliberately separate from the dispatch tool scope above: a sync never runs
    sender-authored free text — the recipient's daemon runs a FIXED, read-only
    digest template — so it can be auto-approved without granting the sender any
    arbitrary execution. A sender's request (see SyncRequest) may only ever
    NARROW this grant (a shorter window, a subset of projects); it can never
    widen `roots`, which is why roots live here (recipient-controlled) and not
    in the request. Dispatch metadata isn't covered by the Ed25519 signature, so
    this recipient-side clamp is what bounds a tampered request.

    Granting dispatch does NOT grant sync — `Scopes.sync` stays None until the
    trustor explicitly enables it (`dispatch sync-grant`).
    """

    enabled: bool = False
    # Directories whose Claude Code transcripts are readable. The agent is
    # confined to these; `..` escapes are rejected on the recipient side.
    roots: list[str] = Field(default_factory=lambda: ["~/.claude/projects"])
    # Allowlist of project names (the URL-encoded cwd dirs under `roots`).
    # [] = every project under `roots`.
    project_allow: list[str] = Field(default_factory=list)
    # Cap on how far back a single request may look. A request asking for more
    # is clamped to this.
    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    # Run unattended (no per-sync Accept gate) — the point of sync. When False,
    # each sync still waits for the recipient's Accept like a normal dispatch.
    auto: bool = True


class Scopes(BaseModel):
    """Per-trust-edge permissions. New edges default to least privilege:
    read-only tools (local read + internet read: WebSearch/WebFetch), no MCP,
    manual approval of every tool call.

    `tools` are the built-in tools; `mcp` is the allowlist of the recipient's
    own MCP servers/tools a sender's dispatch may use — this is what lets a
    dispatch tap the recipient's *powerful* tools (their Notion, their search,
    their domain skills) rather than just the six built-ins. Patterns:
      "notion"               -> any tool from the recipient's `notion` MCP server
      "mcp__github__*"        -> any tool from the `github` server
      "mcp__search__web"      -> one exact MCP tool
    Skills are NOT scoped here: a Skill is just instructions/context and grants
    no capability on its own (any tool it tries to use still passes through the
    `tools`/`mcp`/`paths` gate in can_use_tool). So delegated tasks get all of
    the recipient's Skills; the real sandbox is the tools above.

    The recipient's *dispatch* control plane (sending/inviting/approving) is
    NEVER grantable here — a dispatched task can't re-wield the recipient's
    identity to dispatch onward. That exclusion is enforced in the executor,
    not by this allowlist (see daemon: can_use_tool)."""

    tools: list[str] = Field(default_factory=lambda: ["Read", "Glob", "Grep", "WebSearch", "WebFetch"])
    mcp: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    approval: Literal["manual", "auto"] = "manual"
    # Per-tool "always allow" learned just-in-time on a `manual` edge. Each entry
    # is an EXACT tool name — a built-in ("Bash") or a full MCP tool
    # ("mcp__notion__notion-move-pages") — that skips the per-call approval
    # prompt from then on. Orthogonal to `mcp`: `mcp` decides which servers are
    # even reachable; `auto_tools` decides which already-reachable tools no
    # longer need a human Allow. Grown by the recipient picking "Always allow
    # this tool" on a live approval; never widens reachability on its own.
    auto_tools: list[str] = Field(default_factory=list)
    # What the SENDER's watch view sees of tool results. The agent itself and
    # the recipient's local UI always see full results — this only gates what
    # leaves the recipient's machine for the broker/sender. "redacted" (the
    # default) replaces each tool result's content with a size/status stub;
    # the sender still sees every tool call, each approval decision, and the
    # final reply. "full" streams result contents verbatim.
    result_visibility: Literal["full", "redacted"] = "redacted"
    max_dispatches_per_day: int = Field(default=50, ge=1, le=10000)
    expires_at: Optional[datetime] = None
    # Read-only activity-digest access for `dispatch sync`. None => not granted
    # (the default). Syncs ride this same edge and count against the daily limit.
    sync: Optional[SyncScope] = None

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        unknown = [t for t in value if t not in VALID_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools: {unknown}; valid: {list(VALID_TOOLS)}")
        return value


# Fixed task string a `dispatch sync` rides on. The recipient daemon detects
# metadata['sync'] and IGNORES this as instructions (it runs the digest
# template instead); it's a non-empty sentinel only so the signed envelope —
# which requires a non-empty `task` and covers it — is meaningful in logs.
SYNC_TASK_SENTINEL = "[dispatch:sync] read-only Claude Code activity digest request"


class SyncRequest(BaseModel):
    """Rides on DispatchPayload.metadata['sync'] to mark a dispatch as a
    read-only activity-digest pull rather than a free-text task.

    SECURITY: dispatch metadata is NOT covered by the Ed25519 signature (only
    the task + addressing are), so every field here is sender/broker-supplied
    and untrusted. The recipient daemon only ever lets it NARROW the edge's
    SyncScope — window is clamped, projects are intersected with the allowlist,
    and `roots` come from the SyncScope, never from here.
    """

    window_hours: int = Field(default=24, ge=1, le=24 * 30)
    projects: list[str] = Field(default_factory=list)
    focus: str = Field(default="", max_length=500)


class InvitationCreateRequest(BaseModel):
    """Body for POST /invitations."""

    to_email: str = Field(..., min_length=3, max_length=254)


class AcceptInvitationRequest(BaseModel):
    """Body for POST /invitations/{token}/accept. Omit scopes for defaults."""

    scopes: Optional[Scopes] = None


class TrustScopesUpdate(BaseModel):
    """Body for PATCH /trust/{id}."""

    scopes: Scopes


DispatchEventType = Literal[
    "agent_text",
    "tool_use",
    "tool_result",
    "permission_request",
    "permission_response",
    "dispatch_status",
    "done",
    "error",
    # A human chat message on the dispatch thread. Authored by the sender or the
    # recipient, NOT the agent — it rides the same event trace so it shows in the
    # activity stream, but the executor never reads it back, so it can't steer a
    # run (display-only by construction). data: {id, author, author_role, body,
    # kind}. See build_message_event / MessageCreate.
    "message",
]


class DispatchEvent(TypedDict):
    type: DispatchEventType
    data: dict[str, Any]


# Human-message kinds. "note" is a free reply; "decline_reason" is the optional
# explanation attached when a recipient rejects a dispatch.
MessageKind = Literal["note", "decline_reason"]


class MessageCreate(BaseModel):
    """Body for POST /dispatch/{id}/messages — one human chat message on a
    dispatch thread. The author is the authenticated caller (sender or
    recipient); the broker derives it from the JWT, never from the body."""

    body: str = Field(..., min_length=1, max_length=MESSAGE_MAX_CHARS)
    kind: MessageKind = "note"


def build_message_event(
    *, author: str, author_role: str, body: str, kind: str = "note"
) -> DispatchEvent:
    """A `message` DispatchEvent: a human note pinned to the dispatch thread.
    `id` lets every surface dedupe/key it; `ts` is stamped by whoever persists
    it (broker endpoint / daemon mirror), like every other event."""
    return {
        "type": "message",
        "data": {
            "id": str(uuid4()),
            "author": author,
            "author_role": author_role,
            "body": body,
            "kind": kind,
        },
    }


# --- threading ---------------------------------------------------------------
# A dispatch and its follow-ups form a thread. The linkage lives in metadata
# (unsigned, display-only grouping): `parent_id` points at the dispatch a
# follow-up answers; `thread_id` is the root's id, shared by every dispatch in
# the chain. The follow-up's inherited *content* (parent task/result, cwd) is
# baked into the signed task/context at send time — only the pointers are
# unsigned, so a tampered pointer can at worst mis-group the UI, never widen
# what the agent does.

def parent_id_of(metadata: dict[str, Any] | None) -> Optional[str]:
    pid = (metadata or {}).get("parent_id")
    return str(pid) if pid else None


def thread_id_of(metadata: dict[str, Any] | None, self_id: Any) -> str:
    """The thread this dispatch belongs to: its explicit `thread_id`, else its
    own id (a root dispatch is its own thread)."""
    tid = (metadata or {}).get("thread_id")
    return str(tid) if tid else str(self_id)


def reply_from_events(events: list) -> Optional[str]:
    """The dispatch's "reply" = the agent's final message: the text of the last
    `agent_text` event (which, since `done` is last, is the last one before it).
    Returns None if the run produced no text (e.g. errored before responding).
    This is a read-time derivation — no separate reply is stored or sent."""
    reply: Optional[str] = None
    for e in events or []:
        if isinstance(e, dict) and e.get("type") == "agent_text":
            text = (e.get("data") or {}).get("text")
            if isinstance(text, str) and text.strip():
                reply = text
    return reply


# ============================================================================
# Workflows — n8n-style graphs of local operations, fan-out via dispatch.
# ============================================================================
#
# The workflow is the PAYLOAD: a sender designs a graph of local nodes
# (agent / notify / branch / transform / http / delay / end.*), then
# dispatches the whole graph to N recipients. Each recipient's daemon
# runs its own copy of the engine. There is no `dispatch` node — dispatch
# is the delivery mechanism, not a step in the graph.


class WorkflowNode(BaseModel):
    """One node in a workflow's canvas.

    `type` controls execution: trigger.manual | trigger.cron | agent |
    notify | branch | transform.code | http.request | delay |
    end.success | end.error. Schemaless `params` so node types can evolve.
    """

    id: str = Field(..., min_length=1, max_length=64)
    type: str = Field(..., min_length=1, max_length=64)
    pos: list[float] = Field(default_factory=lambda: [0.0, 0.0], min_length=2, max_length=2)
    params: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    """Directed edge connecting two nodes by their port names."""

    from_node: str = Field(..., alias="from", min_length=1, max_length=64)
    from_port: str = Field(default="out", max_length=32)
    to_node: str = Field(..., alias="to", min_length=1, max_length=64)
    to_port: str = Field(default="in", max_length=32)

    model_config = {"populate_by_name": True}


class WorkflowDefinition(BaseModel):
    """The persisted canvas: nodes + edges."""

    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)


class WorkflowCreateRequest(BaseModel):
    """Body for POST /workflows. Update uses the same shape via PUT."""

    name: str = Field(..., min_length=1, max_length=200)
    definition: WorkflowDefinition = Field(default_factory=WorkflowDefinition)


class WorkflowSummary(BaseModel):
    """List-view representation of a workflow."""

    workflow_id: UUID
    name: str
    node_count: int
    created_at: datetime
    updated_at: datetime


class NodeStatus(str, enum.Enum):
    pending   = "pending"
    running   = "running"
    completed = "completed"
    failed    = "failed"
    skipped   = "skipped"


class NodeState(BaseModel):
    """Per-node execution snapshot inside a run."""

    status: NodeStatus = NodeStatus.pending
    output: Any = None              # whatever the node produced (string, dict)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    error: Optional[str] = None


class WorkflowRunStatus(str, enum.Enum):
    pending   = "pending"
    running   = "running"
    completed = "completed"
    failed    = "failed"
    cancelled = "cancelled"


class WorkflowRunCreateRequest(BaseModel):
    """Body for POST /workflows/{id}/run.

    Sender picks N recipients; the broker creates one dispatch + one run
    row per recipient, each carrying the workflow definition + input as
    a dispatch payload (WorkflowDispatchEnvelope in metadata.workflow).
    """

    recipient_ids: list[str] = Field(..., min_length=1, max_length=50)
    input: dict[str, Any] = Field(default_factory=dict)


class ContextFile(BaseModel):
    """One file in a context pack. Path is workspace-relative."""

    path: str = Field(..., min_length=1, max_length=512)
    content: str = Field(default="")


class ContextCreateRequest(BaseModel):
    """Body for POST /contexts (and PUT /contexts/{id})."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    system_prompt: str = Field(default="")
    files: list[ContextFile] = Field(default_factory=list, max_length=50)


class ContextSummary(BaseModel):
    """List-view representation of a context pack."""

    context_id: UUID
    name: str
    description: str
    file_count: int
    has_system_prompt: bool
    created_at: datetime
    updated_at: datetime


class ContextPack(BaseModel):
    """Full context pack returned by GET /contexts/{id}."""

    context_id: UUID
    owner_id: str
    name: str
    description: str
    system_prompt: str
    files: list[ContextFile]
    created_at: datetime
    updated_at: datetime


class WorkflowDispatchEnvelope(BaseModel):
    """Lives on `DispatchPayload.metadata["workflow"]` when the dispatch
    is carrying a whole workflow to be executed by the recipient's daemon.

    The recipient daemon detects this and runs the engine instead of the
    normal single-prompt agent flow. `run_id` is pre-allocated by the
    broker so the recipient PATCHes a row that already exists.
    """

    run_id: UUID
    workflow_id: UUID
    workflow_name: str = Field(default="")
    definition: WorkflowDefinition
    input: dict[str, Any] = Field(default_factory=dict)


class WorkflowRun(BaseModel):
    """Full execution record returned by GET /runs/{id}."""

    run_id: UUID
    workflow_id: UUID
    triggered_by: str
    status: WorkflowRunStatus
    input: dict[str, Any]
    node_states: dict[str, NodeState]
    error: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
