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

from pydantic import BaseModel, Field, field_validator

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
    """Sender → broker body for POST /dispatch."""

    recipient_id: str = Field(..., min_length=1, max_length=64)
    task: str = Field(..., min_length=1)
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
