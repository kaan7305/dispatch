"""The passwordless /auth/login endpoint is a full impersonation oracle (it
mints a JWT for any username with no verification), so it must be OFF unless
DISPATCH_DEV_AUTH is explicitly set, and ALWAYS refused when Clerk sign-in is
configured — even if the flag is left on. These tests pin that gate.
"""
import pytest
from starlette.testclient import TestClient

from dispatch.broker.app import app
from dispatch.broker.store import STORE
from dispatch.shared.identity import verify_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts from a known env: a JWT secret so issue_token works,
    and no dev-auth / Clerk vars unless the test opts in. STORE.upsert_user is
    stubbed so the enabled path never needs a real Postgres pool."""
    monkeypatch.setenv("DISPATCH_JWT_SECRET", "test-secret-at-least-32-chars-long!!")
    monkeypatch.delenv("DISPATCH_DEV_AUTH", raising=False)
    monkeypatch.delenv("CLERK_FRONTEND_API", raising=False)

    async def _noop_upsert(user_id: str) -> None:
        return None

    monkeypatch.setattr(STORE, "upsert_user", _noop_upsert)


def test_disabled_by_default():
    # No DISPATCH_DEV_AUTH, no Clerk → fail closed.
    resp = client.post("/auth/login", json={"username": "victim@example.com"})
    assert resp.status_code == 403
    assert "DISPATCH_DEV_AUTH" in resp.json()["detail"]


def test_enabled_with_flag(monkeypatch):
    monkeypatch.setenv("DISPATCH_DEV_AUTH", "1")
    resp = client.post("/auth/login", json={"username": "dev@example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "dev@example.com"
    # The issued token is a real, verifiable JWT for that user.
    assert verify_token(body["token"]) == "dev@example.com"


def test_refused_when_clerk_configured_even_with_flag(monkeypatch):
    # A real deployment (Clerk set) must never expose it, flag or not.
    monkeypatch.setenv("DISPATCH_DEV_AUTH", "1")
    monkeypatch.setenv("CLERK_FRONTEND_API", "example.clerk.accounts.dev")
    resp = client.post("/auth/login", json={"username": "victim@example.com"})
    assert resp.status_code == 403
