"""Capability-bucket memory + sender-view redaction.

Memory: edges with identical capability envelopes share a bucket; operational
fields don't fragment it; harvest extracts repo roots deterministically from
tool-call inputs; injection withholds out-of-scope and dead entries.

Redaction: the broker-bound copy of a tool result on a redacted edge is a
size/status stub; the original event (the recipient's local view) is untouched.
"""
from pathlib import Path

from dispatch.daemon import memory
from dispatch.daemon.main import _redact_tool_result
from dispatch.shared.schema import Scopes


# ---------------- capability buckets ----------------

def test_bucket_stable_across_operational_fields():
    base = Scopes(tools=["Read", "Glob"], approval="manual")
    drifted = Scopes(
        tools=["Glob", "Read"],          # order must not matter
        approval="manual",
        auto_tools=["Read"],             # learned JIT — not capability
        max_dispatches_per_day=7,        # rate limit — not capability
        result_visibility="full",        # sender view — not capability
    )
    assert memory.capability_bucket(base) == memory.capability_bucket(drifted)


def test_bucket_changes_with_capability():
    a = Scopes(tools=["Read", "Glob"])
    b = Scopes(tools=["Read", "Glob", "Bash"])
    c = Scopes(tools=["Read", "Glob"], paths=["~/projects"])
    d = Scopes(tools=["Read", "Glob"], approval="auto")
    buckets = {memory.capability_bucket(s) for s in (a, b, c, d)}
    assert len(buckets) == 4


# ---------------- harvest ----------------

def _git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    return repo


def test_harvest_records_repo_roots_of_successful_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / "mem")
    repo = _git_repo(tmp_path, "yuni")

    h = memory.RunHarvester()
    # Successful read inside the repo → repo root remembered.
    h.observe({"type": "tool_use",
               "data": {"id": "t1", "name": "Read",
                        "input": {"file_path": str(repo / "src" / "app.py")}}})
    h.observe({"type": "tool_result",
               "data": {"tool_use_id": "t1", "content": "x = 1", "is_error": False}})
    # Failed call → its path must NOT be remembered.
    other = _git_repo(tmp_path, "secret")
    h.observe({"type": "tool_use",
               "data": {"id": "t2", "name": "Read",
                        "input": {"file_path": str(other / "src" / "app.py")}}})
    h.observe({"type": "tool_result",
               "data": {"tool_use_id": "t2", "content": "denied", "is_error": True}})
    h.finish("bucket1")

    paths = {e["path"] for e in memory.load_entries("bucket1")}
    assert str(repo) in paths
    assert str(other) not in paths


def test_harvest_counts_hits_and_records_pinned_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / "mem")
    repo = _git_repo(tmp_path, "yuni")
    pinned = tmp_path / "plain-project"   # no .git — only reachable via pin
    pinned.mkdir()

    for _ in range(2):
        h = memory.RunHarvester()
        h.observe({"type": "tool_use",
                   "data": {"id": "t1", "name": "Glob",
                            "input": {"path": str(repo), "pattern": "*"}}})
        h.observe({"type": "tool_result",
                   "data": {"tool_use_id": "t1", "content": "src", "is_error": False}})
        h.finish("b", run_cwd=pinned)

    entries = {e["path"]: e for e in memory.load_entries("b")}
    assert entries[str(repo)]["hits"] == 2
    assert str(pinned) in entries


def test_remove_entry_and_clear_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / "mem")
    repo = _git_repo(tmp_path, "yuni")
    other = _git_repo(tmp_path, "other")

    h = memory.RunHarvester()
    for i, r in enumerate((repo, other)):
        h.observe({"type": "tool_use",
                   "data": {"id": f"t{i}", "name": "Glob",
                            "input": {"path": str(r), "pattern": "*"}}})
        h.observe({"type": "tool_result",
                   "data": {"tool_use_id": f"t{i}", "content": "ok", "is_error": False}})
    h.finish("b")

    assert memory.remove_entry("b", str(repo)) is True
    assert memory.remove_entry("b", str(repo)) is False  # already gone
    assert {e["path"] for e in memory.load_entries("b")} == {str(other)}

    memory.clear_bucket("b")
    assert memory.load_entries("b") == []
    memory.clear_bucket("b")  # idempotent on a missing file


# ---------------- injection ----------------

def test_prompt_filters_dead_and_out_of_scope_paths(tmp_path):
    live_in = tmp_path / "in-scope"
    live_out = tmp_path / "elsewhere"
    live_in.mkdir()
    live_out.mkdir()
    entries = [
        {"path": str(live_in), "last_seen": "2026-06-10T00:00:00+00:00"},
        {"path": str(live_out), "last_seen": "2026-06-10T00:00:00+00:00"},
        {"path": str(tmp_path / "deleted"), "last_seen": "2026-06-10T00:00:00+00:00"},
    ]
    # Path-restricted edge: only entries under the allowlist are injected.
    prompt = memory.memory_prompt(entries, [live_in], paths_restricted=True)
    assert str(live_in) in prompt
    assert str(live_out) not in prompt
    assert "deleted" not in prompt
    # Unrestricted edge: every live entry is injected.
    prompt = memory.memory_prompt(entries, [], paths_restricted=False)
    assert str(live_in) in prompt and str(live_out) in prompt
    # Nothing live → no block at all (don't waste prompt on an empty header).
    assert memory.memory_prompt(
        [{"path": str(tmp_path / "gone")}], [], paths_restricted=False
    ) is None


# ---------------- redaction ----------------

def test_redacted_copy_is_stub_and_original_untouched():
    original = {
        "type": "tool_result",
        "data": {"tool_use_id": "t1", "content": "SECRET FILE CONTENTS",
                 "is_error": False, "ts": "2026-06-10T00:00:00+00:00"},
    }
    red = _redact_tool_result(original)
    assert red["data"]["redacted"] is True
    assert "SECRET" not in red["data"]["content"]
    assert f"{len('SECRET FILE CONTENTS')} bytes" in red["data"]["content"]
    assert "ok" in red["data"]["content"]
    # Metadata the sender legitimately needs survives.
    assert red["data"]["tool_use_id"] == "t1"
    assert red["data"]["ts"] == original["data"]["ts"]
    # The recipient's local copy is a different object, still intact.
    assert original["data"]["content"] == "SECRET FILE CONTENTS"
    assert "redacted" not in original["data"]


def test_redacted_copy_keeps_error_status():
    red = _redact_tool_result(
        {"type": "tool_result",
         "data": {"tool_use_id": "t2", "content": "boom", "is_error": True}}
    )
    assert red["data"]["is_error"] is True
    assert "error" in red["data"]["content"]


def test_default_scope_is_redacted():
    assert Scopes().result_visibility == "redacted"
