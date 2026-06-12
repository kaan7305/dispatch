"""Tests for human-message + threading plumbing (reply / decline-reason /
follow-up). These cover the pure pieces that don't need Postgres or a live
daemon: the message event shape, the thread/parent helpers, MessageCreate
validation, and the follow-up metadata builder.
"""
import pytest
from pydantic import ValidationError

from dispatch.shared.schema import (
    MESSAGE_MAX_CHARS,
    MessageCreate,
    build_message_event,
    parent_id_of,
    reply_from_events,
    thread_id_of,
)
from dispatch.daemon.local_app import _build_followup_metadata


def test_message_event_shape():
    ev = build_message_event(
        author="a@x", author_role="recipient", body="done — redacted secrets", kind="note"
    )
    assert ev["type"] == "message"
    d = ev["data"]
    assert d["author"] == "a@x"
    assert d["author_role"] == "recipient"
    assert d["body"] == "done — redacted secrets"
    assert d["kind"] == "note"
    assert d["id"]  # a stable id for dedupe/keying


def test_two_messages_get_distinct_ids():
    a = build_message_event(author="x", author_role="sender", body="hi")
    b = build_message_event(author="x", author_role="sender", body="hi")
    assert a["data"]["id"] != b["data"]["id"]


def test_message_event_ignored_by_reply_derivation():
    # A human note must never be mistaken for the agent's reply.
    events = [
        {"type": "agent_text", "data": {"text": "the real answer"}},
        {"type": "message", "data": {"author": "x", "body": "nice, thanks!"}},
        {"type": "done", "data": {}},
    ]
    assert reply_from_events(events) == "the real answer"


def test_thread_helpers():
    # A root dispatch is its own thread; a child points at the root.
    assert thread_id_of({}, "root-1") == "root-1"
    assert thread_id_of({"thread_id": "root-1"}, "child-2") == "root-1"
    assert parent_id_of({"parent_id": "root-1"}) == "root-1"
    assert parent_id_of({}) is None
    assert parent_id_of(None) is None


def test_message_create_validation():
    assert MessageCreate(body="hi").kind == "note"
    assert MessageCreate(body="no", kind="decline_reason").kind == "decline_reason"
    with pytest.raises(ValidationError):
        MessageCreate(body="")  # empty rejected
    with pytest.raises(ValidationError):
        MessageCreate(body="x" * (MESSAGE_MAX_CHARS + 1))  # over the cap
    with pytest.raises(ValidationError):
        MessageCreate(body="x", kind="bogus")  # unknown kind


def test_followup_metadata_inherits_and_threads():
    parent = {
        "dispatch_id": "P",
        "task": "write the parser",
        "reply": "parser written in parse.py",
        "metadata": {"cwd": "/repo", "thread_id": "ROOT"},
    }
    meta = _build_followup_metadata(parent, {}, "P")
    assert meta["parent_id"] == "P"
    assert meta["thread_id"] == "ROOT"          # inherits the root's thread
    assert meta["cwd"] == "/repo"               # inherits the working dir
    bg = meta["context"]["background"]
    assert "write the parser" in bg             # parent task woven in
    assert "parser written in parse.py" in bg   # parent result woven in


def test_followup_root_becomes_thread_when_parent_has_none():
    parent = {"dispatch_id": "P", "task": "t", "reply": None, "metadata": {}}
    meta = _build_followup_metadata(parent, {}, "P")
    assert meta["thread_id"] == "P"             # parent is the thread root
    assert "cwd" not in meta                    # nothing to inherit
    assert "Result of that dispatch" not in meta["context"]["background"]


def test_followup_does_not_clobber_explicit_background():
    parent = {"dispatch_id": "P", "task": "t", "reply": "r", "metadata": {}}
    meta = _build_followup_metadata(parent, {"context": {"background": "mine"}}, "P")
    assert meta["context"]["background"] == "mine"
