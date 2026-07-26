"""Which surface asks the human to approve a gated tool call (Layer 3).

Dispatch's front door runs inside whatever agent the user sits in, and hosts do
not agree on how a server may ask a question. Claude Code can be handed an
`approval_needed` payload and will relay it to AskUserQuestion; Codex has no
such tool but does render MCP elicitation; a non-interactive client can do
neither. Picking wrong is not a cosmetic bug — it decides whether a person ever
sees the prompt, and Layer 3 is the layer that exists so a machine can't consent
on their behalf.

Two properties are pinned here:

  1. **The surface follows the host, and an unknown host does not regress
     Claude.** Host detection walks optional attributes on an SDK-owned session
     object; if the mcp release renames one, the answer must fall back to
     today's behavior rather than throw or silently switch surfaces.

  2. **A client that answers for the user has not produced consent.** Codex
     auto-approves elicitations in danger-full-access sandbox mode, and an
     elicitation response carries no proof a human read it. An approval that
     returns faster than a person could read the call is thrown away and the
     run's approvals move to the daemon's own surface. A fast *denial* is
     honored — denying is the safe direction.
"""
import asyncio

import pytest

from dispatch import mcp_server


@pytest.fixture(autouse=True)
def no_ambient_config(monkeypatch):
    """Neither the developer's ~/.dispatch/config.json nor their environment may
    decide what these tests observe."""
    monkeypatch.delenv("DISPATCH_APPROVAL_UI", raising=False)
    monkeypatch.setattr(mcp_server, "_load_config", dict)


class FakeCtx:
    """The slice of mcp's Context that _supervise touches.

    `elicit_delay` fakes how long the client took to answer: a real human read
    the call, a client in bypass mode did not."""

    def __init__(self, host="codex", answer="1. Allow once", elicit_delay=0.0, elicit_raises=None):
        self.session = type(
            "S", (), {"client_params": type(
                "P", (), {"clientInfo": type("I", (), {"name": host})()})()}
        )()
        self.answer = answer
        self.elicit_delay = elicit_delay
        self.elicit_raises = elicit_raises
        self.elicits = 0
        self.infos = []

    async def elicit(self, message, schema):
        self.elicits += 1
        if self.elicit_raises is not None:
            raise self.elicit_raises
        if self.elicit_delay:
            await asyncio.sleep(self.elicit_delay)
        return _Accepted(self.answer)

    async def info(self, message):
        self.infos.append(message)


class _Accepted:
    """Stands in for mcp's AcceptedElicitation. _supervise isinstance-checks the
    real class, so the test patches that check to accept this one."""

    def __init__(self, decision):
        self.data = type("D", (), {"decision": decision})()


# ----------------------------------------------------------------------------
# Host → surface
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("host, expected", [
    ("claude-code", "picker"),   # has AskUserQuestion
    ("Claude Code", "picker"),   # name casing is not load-bearing
    ("codex", "form"),           # renders elicitation, no picker tool
    ("codex-cli", "form"),
    ("cursor", "form"),          # any other client: ask inline rather than relay
    ("", "picker"),              # unknown → today's behavior, don't regress Claude
])
def test_surface_follows_the_host(host, expected):
    assert mcp_server._approval_ui(FakeCtx(host=host)) == expected


def test_no_context_falls_back_to_picker():
    """Callers without a ctx (and any future SDK rename that hides clientInfo)
    must land on the pre-detection default."""
    assert mcp_server._approval_ui(None) == "picker"


def test_a_broken_session_object_does_not_raise():
    broken = type("C", (), {"session": None})()
    assert mcp_server._host_name(broken) == ""
    assert mcp_server._approval_ui(broken) == "picker"


@pytest.mark.parametrize("value", ["picker", "form", "local"])
def test_explicit_config_overrides_the_host(monkeypatch, value):
    monkeypatch.setenv("DISPATCH_APPROVAL_UI", value)
    # A Claude host asked for 'local' gets 'local'; a Codex host asked for
    # 'picker' gets 'picker'. The operator's choice wins either way.
    assert mcp_server._approval_ui(FakeCtx(host="claude-code")) == value
    assert mcp_server._approval_ui(FakeCtx(host="codex")) == value


def test_an_unrecognized_config_value_is_ignored(monkeypatch):
    monkeypatch.setenv("DISPATCH_APPROVAL_UI", "yolo")
    assert mcp_server._approval_ui(FakeCtx(host="codex")) == "form"


def test_decision_labels_map_to_daemon_verbs():
    assert mcp_server._decision_of("1. Allow once") == "allow"
    assert mcp_server._decision_of("2. Always allow this tool") == "always"
    assert mcp_server._decision_of("3. Allow for this session") == "session"
    assert mcp_server._decision_of("4. Deny") == "deny"
    assert mcp_server._decision_of("5. Decline the dispatch") == "deny"
    # A declined prompt, an empty answer, or a client that invented its own
    # response all land on the safe direction.
    assert mcp_server._decision_of("") == "deny"
    assert mcp_server._decision_of("sure, go ahead") == "deny"


# ----------------------------------------------------------------------------
# Fabricated consent
# ----------------------------------------------------------------------------

@pytest.fixture
def fake_daemon(monkeypatch):
    """A daemon with one pending tool call that goes terminal after a few polls.

    Records every call so a test can assert on what was (and wasn't) POSTed —
    the decision endpoint is the only place consent becomes real."""
    calls = []

    async def _local_call(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET" and path.startswith("/api/dispatch/"):
            polls = sum(1 for c in calls if c[0] == "GET")
            if polls >= 4:
                return {"status": "completed", "events": [1, 2], "sender_id": "kaan",
                        "pending_tools": {}}
            return {
                "status": "running",
                "events": [1],
                "sender_id": "kaan",
                "pending_tools": {"req-1": {"tool": "Bash", "input": {"command": "rm -rf ~"}}},
            }
        return {}

    monkeypatch.setattr(mcp_server, "_local_call", _local_call)
    monkeypatch.setattr(mcp_server, "AcceptedElicitation", _Accepted)
    return calls


def _decisions(calls):
    return [(p, body) for m, p, body in calls if m == "POST" and "/decision" in p]


def test_instant_approval_is_not_treated_as_consent(fake_daemon):
    """The Codex danger-full-access case: the client answers 'Allow once'
    immediately. Nothing may be posted, and the run's approvals must move to a
    surface the human actually sees."""
    ctx = FakeCtx(host="codex", answer="1. Allow once", elicit_delay=0.0)
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert _decisions(fake_daemon) == []            # no fabricated allow reached the daemon
    assert result["approval_surface"] == "local"    # latched off the untrusted surface
    assert ctx.elicits == 1                         # and never re-prompted the same client
    assert ctx.infos, "the human should be told where the approval is waiting"
    assert "denied" not in result["note"].lower() or "local" in result["note"].lower()


def test_a_read_approval_is_posted(fake_daemon):
    """The ordinary Codex path: a human read the call and chose. That decision
    goes straight through — the guard must not make elicitation unusable."""
    ctx = FakeCtx(
        host="codex", answer="1. Allow once",
        elicit_delay=mcp_server._HUMAN_READ_FLOOR_S + 0.1,
    )
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert _decisions(fake_daemon) == [("/api/dispatch/d-1/tool/req-1/decision",
                                       {"decision": "allow"})]
    assert result["approval_surface"] == "form"


def test_instant_denial_is_honored(fake_daemon):
    """Denying is the safe direction: a client that declines on the user's behalf
    produces the same outcome as a timeout, so there is nothing to protect
    against and no reason to make them wait."""
    ctx = FakeCtx(host="codex", answer="4. Deny", elicit_delay=0.0)
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert _decisions(fake_daemon) == [("/api/dispatch/d-1/tool/req-1/decision",
                                       {"decision": "deny"})]
    assert result["approval_surface"] == "form"


def test_unavailable_elicitation_on_a_pickerless_host_goes_local(fake_daemon):
    """A client that implements neither surface still must not silently deny or
    cancel: the daemon holds the future and its own UI can collect the answer."""
    ctx = FakeCtx(host="codex", elicit_raises=RuntimeError("elicitation not supported"))
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert _decisions(fake_daemon) == []
    assert result["approval_surface"] == "local"
    assert ctx.infos


def test_unavailable_elicitation_on_a_picker_host_relays(fake_daemon, monkeypatch):
    """A Claude user who pinned form mode, on a surface that can't render it,
    still gets the picker relay rather than being pushed off-session."""
    monkeypatch.setenv("DISPATCH_APPROVAL_UI", "form")
    ctx = FakeCtx(host="claude-code", elicit_raises=RuntimeError("no elicitation here"))
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert result["status"] == "approval_needed"
    assert result["request_id"] == "req-1"
    assert _decisions(fake_daemon) == []


def test_local_mode_never_asks_in_session(fake_daemon, monkeypatch):
    monkeypatch.setenv("DISPATCH_APPROVAL_UI", "local")
    ctx = FakeCtx(host="codex")
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert ctx.elicits == 0
    assert _decisions(fake_daemon) == []
    assert result["approval_surface"] == "local"
    assert len(ctx.infos) == 1, "announce the wait once, not on every poll"


def test_picker_mode_returns_the_relay_payload(fake_daemon):
    ctx = FakeCtx(host="claude-code")
    result = asyncio.run(mcp_server._supervise("d-1", ctx))

    assert result["status"] == "approval_needed"
    assert result["tool"] == "Bash"
    assert result["input"] == {"command": "rm -rf ~"}
    assert ctx.elicits == 0
