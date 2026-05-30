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

VALID_TOOLS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DispatchStatus(str, enum.Enum):
    pending = "pending"        # broker has it, recipient daemon offline
    delivered = "delivered"    # daemon pulled it from broker
    accepted = "accepted"      # recipient pressed Accept
    running = "running"        # agent session active
    completed = "completed"    # agent finished cleanly
    denied = "denied"          # recipient pressed Reject (top-level)
    failed = "failed"          # exception during execution
    expired = "expired"        # past expires_at without acceptance
    cancelled = "cancelled"    # trust edge revoked while in-flight


class DispatchPayload(BaseModel):
    """The signed-over, cross-party dispatch envelope."""

    dispatch_id: UUID = Field(default_factory=uuid4)
    sender_id: str = Field(..., min_length=1, max_length=64)
    recipient_id: str = Field(..., min_length=1, max_length=64)
    task: str = Field(..., min_length=1)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class DispatchCreateRequest(BaseModel):
    """Sender → broker body for POST /dispatch.

    Provide either `recipient_id` (single) or `recipient_ids` (fan-out).
    The broker runs the trust check + signing flow for each recipient
    independently and returns one result per recipient.
    """

    recipient_id: Optional[str] = Field(default=None, max_length=64)
    recipient_ids: Optional[list[str]] = Field(default=None, max_length=50)
    task: str = Field(..., min_length=1)
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)
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


class DeviceEnrollRequest(BaseModel):
    """Daemon → broker body for POST /devices/enroll."""

    label: str = Field(..., min_length=1, max_length=128)
    public_key: str = Field(..., description="Ed25519 public key, base64-encoded")


class Scopes(BaseModel):
    """Per-trust-edge permissions. New edges default to least privilege:
    read-only tools, manual approval of every tool call."""

    tools: list[str] = Field(default_factory=lambda: ["Read", "Glob", "Grep"])
    paths: list[str] = Field(default_factory=list)
    approval: Literal["manual", "auto"] = "manual"
    max_dispatches_per_day: int = Field(default=50, ge=1, le=10000)
    expires_at: Optional[datetime] = None

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        unknown = [t for t in value if t not in VALID_TOOLS]
        if unknown:
            raise ValueError(f"unknown tools: {unknown}; valid: {list(VALID_TOOLS)}")
        return value


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
]


class DispatchEvent(TypedDict):
    type: DispatchEventType
    data: dict[str, Any]


# ============================================================================
# Workflows — n8n-style visual chains of dispatches.
# ============================================================================


class WorkflowNode(BaseModel):
    """One node in a workflow's canvas.

    `type` controls execution: trigger.manual | dispatch | notify | wait_reply.
    `params` is the per-type parameter bag (e.g. recipient_id + task for
    dispatch nodes). We keep it schema-less so node types can evolve.
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
    dispatch_id: Optional[UUID] = None   # for dispatch nodes — linked dispatch
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
    """Body for POST /workflows/{id}/run."""

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
