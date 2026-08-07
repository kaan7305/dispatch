"""Multi-broker client behavior: combined inbox merge and action routing.

Two layers under test, each with the brokers stubbed out:

  * CLI (`dispatch inbox/send/status/...`): `cli._request` is replaced with a
    fake keyed by broker URL, so no HTTP happens. Verifies merge + provenance
    tagging, edge-home routing for send, --broker disambiguation, and probe
    routing for broker-local ids (dispatch ids, invite tokens).
  * Daemon local API (`/api/dispatches`, `/api/compose`): the pooled
    `broker_request` is monkeypatched; verifies the same aggregation/routing
    the SPA and MCP ride on.
"""
import json

import httpx
import pytest

import dispatch.cli as cli
from dispatch.shared import config as shared_config
from dispatch.shared.config import BrokerLink

BROKER_A = "https://a.example"
BROKER_B = "https://b.example"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCH_HOME", str(tmp_path))
    monkeypatch.delenv("DISPATCH_BROKER", raising=False)
    monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
    shared_config.upsert_broker(BROKER_A, token="jwt-a", label="home")
    shared_config.upsert_broker(BROKER_B, token="jwt-b", label="work")
    return tmp_path


class FakeBrokers:
    """Route (broker, method, path) → canned responses; record every call."""

    def __init__(self, routes):
        self.routes = routes  # {(broker, "METHOD path"): data | CliError}
        self.calls = []

    def __call__(self, broker, token, method, path, **kw):
        self.calls.append((broker, method, path, kw))
        key = (broker, f"{method} {path}")
        if key not in self.routes:
            raise cli.BrokerError(f"broker error 404: not found", 404)
        result = self.routes[key]
        if isinstance(result, Exception):
            raise result
        return json.loads(json.dumps(result))  # deep copy


def _run(monkeypatch, fake, argv):
    monkeypatch.setattr(cli, "_request", fake)
    return cli.main(argv)


def test_inbox_merges_sorts_and_tags(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "GET /dispatches"): {"dispatches": [
            {"dispatch_id": "aaa-1", "status": "completed", "sender_id": "kaan",
             "task": "t1", "created_at": "2026-08-01T00:00:00+00:00"},
        ]},
        (BROKER_B, "GET /dispatches"): {"dispatches": [
            {"dispatch_id": "bbb-1", "status": "pending", "sender_id": "jeff",
             "task": "t2", "created_at": "2026-08-05T00:00:00+00:00"},
        ]},
    })
    assert _run(monkeypatch, fake, ["inbox", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    items = out["dispatches"]
    # Merged across both brokers, newest first, each tagged with home broker.
    assert [d["dispatch_id"] for d in items] == ["bbb-1", "aaa-1"]
    assert items[0]["broker_url"] == BROKER_B
    assert items[0]["broker_label"] == "work"
    assert items[1]["broker_url"] == BROKER_A


def test_inbox_degrades_when_one_broker_is_down(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "GET /dispatches"): cli.CliError("can't reach broker"),
        (BROKER_B, "GET /dispatches"): {"dispatches": [
            {"dispatch_id": "bbb-1", "status": "pending", "sender_id": "jeff",
             "task": "t", "created_at": "2026-08-05T00:00:00+00:00"},
        ]},
    })
    assert _run(monkeypatch, fake, ["inbox", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [d["dispatch_id"] for d in out["dispatches"]] == ["bbb-1"]
    assert out["broker_errors"][0]["broker_url"] == BROKER_A


def test_send_routes_to_edge_home_broker(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "GET /trust"): {"trust": []},
        (BROKER_B, "GET /trust"): {"trust": [
            {"direction": "outgoing", "peer": "kaan@x.com"},
        ]},
        (BROKER_B, "POST /dispatch"): {"dispatch_id": "d-1", "status": "pending"},
    })
    assert _run(monkeypatch, fake, ["send", "kaan@x.com", "do the thing", "--json"]) == 0
    # The POST went to the broker holding the edge, not the primary.
    assert (BROKER_B, "POST", "/dispatch") in [c[:3] for c in fake.calls]
    assert (BROKER_A, "POST", "/dispatch") not in [c[:3] for c in fake.calls]
    assert json.loads(capsys.readouterr().out)["broker_url"] == BROKER_B


def test_send_ambiguous_contact_requires_broker_flag(home, monkeypatch, capsys):
    edge = {"trust": [{"direction": "outgoing", "peer": "kaan@x.com"}]}
    fake = FakeBrokers({
        (BROKER_A, "GET /trust"): edge,
        (BROKER_B, "GET /trust"): edge,
    })
    assert _run(monkeypatch, fake, ["send", "kaan@x.com", "task"]) == 1
    err = capsys.readouterr().err
    assert "multiple brokers" in err and "--broker" in err
    # Nothing was sent anywhere.
    assert all(c[1] != "POST" for c in fake.calls)


def test_send_with_broker_flag_pins_target(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "POST /dispatch"): {"dispatch_id": "d-2", "status": "pending"},
    })
    assert _run(monkeypatch, fake,
                ["send", "kaan@x.com", "task", "--broker", BROKER_A, "--json"]) == 0
    # Pinned: no /trust lookup fan-out, straight to the named broker.
    assert [c[:3] for c in fake.calls] == [(BROKER_A, "POST", "/dispatch")]


def test_send_no_edge_anywhere_errors(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "GET /trust"): {"trust": []},
        (BROKER_B, "GET /trust"): {"trust": []},
    })
    assert _run(monkeypatch, fake, ["send", "nobody@x.com", "task"]) == 1
    assert "no outgoing trust edge" in capsys.readouterr().err


def test_status_probes_brokers_until_owner_found(home, monkeypatch, capsys):
    fake = FakeBrokers({
        # A answers 404 (not its dispatch); B owns it.
        (BROKER_B, "GET /dispatch/11111111-1111-1111-1111-111111111111"): {
            "dispatch_id": "11111111-1111-1111-1111-111111111111",
            "status": "completed", "sender_id": "kaan", "recipient_id": "me",
            "task": "t", "created_at": "x", "expires_at": "y",
        },
    })
    assert _run(monkeypatch, fake,
                ["status", "11111111-1111-1111-1111-111111111111", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["broker_url"] == BROKER_B


def test_accept_invitation_probes_home_broker(home, monkeypatch, capsys):
    fake = FakeBrokers({
        # Invite token only exists on B; A 404s.
        (BROKER_B, "POST /invitations/tok-123/accept"): {
            "status": "accepted", "trust_link_id": "edge-1",
        },
    })
    assert _run(monkeypatch, fake,
                ["accept-invitation", "tok-123", "--tools", "Read,Glob,Grep",
                 "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trust_link_id"] == "edge-1"
    assert out["broker_url"] == BROKER_B


def test_contacts_are_tagged_with_home_broker(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "GET /trust"): {"trust": [
            {"trust_link_id": "e-a", "direction": "incoming", "peer": "kaan",
             "peer_online": True, "scopes": {"tools": ["Read"], "approval": "manual"}},
        ]},
        (BROKER_B, "GET /trust"): {"trust": [
            {"trust_link_id": "e-b", "direction": "outgoing", "peer": "jeff",
             "peer_online": False, "scopes": {"tools": ["Read"], "approval": "auto"}},
        ]},
    })
    assert _run(monkeypatch, fake, ["contacts", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    by_id = {e["trust_link_id"]: e for e in out["trust"]}
    assert by_id["e-a"]["broker_url"] == BROKER_A
    assert by_id["e-b"]["broker_url"] == BROKER_B


def test_set_scope_routes_to_edge_home_broker(home, monkeypatch, capsys):
    fake = FakeBrokers({
        (BROKER_A, "GET /trust"): {"trust": []},
        (BROKER_B, "GET /trust"): {"trust": [
            {"trust_link_id": "e-b", "direction": "incoming", "peer": "jeff",
             "can_edit_scopes": True, "scopes": {"tools": ["Read"]}},
        ]},
        (BROKER_B, "PATCH /trust/e-b"): {"status": "ok"},
    })
    assert _run(monkeypatch, fake,
                ["set-scope", "e-b", "--tools", "Read,Grep", "--json"]) == 0
    assert (BROKER_B, "PATCH", "/trust/e-b") in [c[:3] for c in fake.calls]


# ── Daemon local API (what the SPA + MCP ride on) ────────────────────────────


def _fake_broker_request(routes):
    """Monkeypatch target for local_app.broker_request; routes on full URL."""
    calls = []

    async def fake(client, method, url, *, json_body=None, params=None,
                   headers=None, timeout=None):
        calls.append((method, url, json_body))
        key = (method, url)
        if key in routes:
            return httpx.Response(200, json=routes[key],
                                  headers={"content-type": "application/json"})
        return httpx.Response(404, json={"detail": "not found"},
                              headers={"content-type": "application/json"})

    fake.calls = calls
    return fake


@pytest.fixture
def local_client(home, monkeypatch):
    from fastapi.testclient import TestClient
    import dispatch.daemon.local_app as la
    from dispatch.daemon.main import DaemonState

    links = [
        BrokerLink(url=BROKER_A, token="jwt-a", label="home", connected=True),
        BrokerLink(url=BROKER_B, token="jwt-b", label="work", connected=True),
    ]
    state = la.LocalState(user_id="me@x.com", broker_url=BROKER_A,
                          broker_token="jwt-a", brokers=links)

    def make(routes):
        fake = _fake_broker_request(routes)
        monkeypatch.setattr(la, "broker_request", fake)
        app = la.make_app(state, DaemonState(), "local-tok")
        client = TestClient(app)
        client.headers["Authorization"] = "Bearer local-tok"
        return client, fake, state

    return make


def test_local_api_dispatches_aggregates_and_tags(local_client):
    client, fake, _ = local_client({
        ("GET", f"{BROKER_A}/dispatches"): {"dispatches": [
            {"dispatch_id": "a-1", "created_at": "2026-08-01T00:00:00+00:00"},
        ]},
        ("GET", f"{BROKER_B}/dispatches"): {"dispatches": [
            {"dispatch_id": "b-1", "created_at": "2026-08-06T00:00:00+00:00"},
        ]},
    })
    body = client.get("/api/dispatches?role=received").json()
    assert [d["dispatch_id"] for d in body["dispatches"]] == ["b-1", "a-1"]
    assert body["dispatches"][0]["broker_label"] == "work"
    assert body["dispatches"][1]["broker_url"] == BROKER_A


def test_local_api_compose_routes_to_edge_broker(local_client):
    client, fake, state = local_client({
        ("GET", f"{BROKER_A}/trust"): {"trust": []},
        ("GET", f"{BROKER_B}/trust"): {"trust": [
            {"direction": "outgoing", "peer": "kaan@x.com"},
        ]},
        ("POST", f"{BROKER_B}/dispatch"): {
            "dispatch_id": "11111111-1111-1111-1111-111111111111",
            "status": "pending",
        },
    })
    r = client.post("/api/compose", json={"recipient_id": "kaan@x.com", "task": "t"})
    assert r.status_code == 200
    assert ("POST", f"{BROKER_B}/dispatch") in [c[:2] for c in fake.calls]
    assert ("POST", f"{BROKER_A}/dispatch") not in [c[:2] for c in fake.calls]
    # The new dispatch's home broker is remembered for status/cancel routing.
    from uuid import UUID
    assert state.broker_of[UUID("11111111-1111-1111-1111-111111111111")].url == BROKER_B


def test_local_api_compose_ambiguous_is_409(local_client):
    edge = {"trust": [{"direction": "outgoing", "peer": "kaan@x.com"}]}
    client, fake, _ = local_client({
        ("GET", f"{BROKER_A}/trust"): edge,
        ("GET", f"{BROKER_B}/trust"): edge,
    })
    r = client.post("/api/compose", json={"recipient_id": "kaan@x.com", "task": "t"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "ambiguous_broker"
    assert {b["url"] for b in detail["brokers"]} == {BROKER_A, BROKER_B}
    # Nothing was sent.
    assert all(m != "POST" for m, _u, _b in fake.calls)


def test_local_api_compose_explicit_broker_url_wins(local_client):
    client, fake, _ = local_client({
        ("POST", f"{BROKER_A}/dispatch"): {"dispatch_id": None, "status": "pending"},
    })
    r = client.post("/api/compose", json={
        "recipient_id": "kaan@x.com", "task": "t", "broker_url": BROKER_A,
    })
    assert r.status_code == 200
    assert [c[:2] for c in fake.calls] == [("POST", f"{BROKER_A}/dispatch")]


def test_local_api_cancel_probes_to_owner(local_client):
    client, fake, _ = local_client({
        ("POST", f"{BROKER_B}/dispatch/22222222-2222-2222-2222-222222222222/cancel"):
            {"status": "cancelled"},
    })
    r = client.post("/api/dispatch/22222222-2222-2222-2222-222222222222/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_local_state_tags_new_dispatches(home):
    from datetime import datetime, timezone
    from uuid import uuid4
    import dispatch.daemon.local_app as la
    from dispatch.shared.schema import DispatchPayload

    link = BrokerLink(url=BROKER_B, token="jwt-b", label="work")
    state = la.LocalState(brokers=[link])
    payload = DispatchPayload(
        dispatch_id=uuid4(), sender_id="kaan", recipient_id="me", task="t",
        created_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc),
    )
    state.on_new_dispatch(payload, {}, broker=link)
    summary = la._entry_summary(state.entries[payload.dispatch_id])
    assert summary["broker_url"] == BROKER_B
    assert summary["broker_label"] == "work"
    assert state.broker_for(payload.dispatch_id) is link
