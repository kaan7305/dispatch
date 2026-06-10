"""machine_index: the daemon-side resolver that starts a delegated agent in
the right directory on any machine — scan, cache, deterministic matching,
and scope filtering."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatch.daemon import machine_index


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """A throwaway home with a few projects, plus an isolated index file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(machine_index, "INDEX_FILE", tmp_path / "index.json")

    def project(rel: str, marker: str = ".git") -> Path:
        d = home / rel
        d.mkdir(parents=True)
        m = d / marker
        m.mkdir() if marker == ".git" else m.write_text("")
        return d

    project("Desktop/yuni")
    project("Desktop/dispatch", marker="pyproject.toml")
    project("Documents/work/billing", marker="package.json")
    # Noise that must NOT be indexed:
    (home / "Library/big").mkdir(parents=True)          # skipped root
    (home / ".hiddenrepo/.git").mkdir(parents=True)     # hidden
    (home / "Desktop/empty").mkdir()                    # no marker
    return home


def test_scan_finds_projects_and_skips_noise(fake_home):
    paths = {p["path"] for p in machine_index._scan()}
    assert paths == {
        str(fake_home / "Desktop/yuni"),
        str(fake_home / "Desktop/dispatch"),
        str(fake_home / "Documents/work/billing"),
    }


def test_scan_does_not_descend_into_projects(fake_home):
    nested = fake_home / "Desktop/yuni/vendor/inner"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    paths = {p["path"] for p in machine_index._scan()}
    assert str(nested) not in paths
    assert str(fake_home / "Desktop/yuni") in paths


def test_projects_caches_to_index_file(fake_home):
    first = machine_index.projects()
    assert machine_index.INDEX_FILE.exists()
    # A project added after the scan is invisible until the TTL lapses…
    late = fake_home / "Desktop/late"
    late.mkdir()
    (late / ".git").mkdir()
    assert {p["path"] for p in machine_index.projects()} == {
        p["path"] for p in first
    }
    # …and visible on a forced refresh.
    assert str(late) in {p["path"] for p in machine_index.projects(refresh=True)}


def test_projects_ignores_corrupt_cache(fake_home):
    machine_index.INDEX_FILE.write_text("not json")
    assert {p["path"] for p in machine_index.projects()} == {
        str(fake_home / "Desktop/yuni"),
        str(fake_home / "Desktop/dispatch"),
        str(fake_home / "Documents/work/billing"),
    }


def test_match_task_single_confident_hit(fake_home):
    projs = machine_index.projects(refresh=True)
    hit = machine_index.match_task("summarize the postermaking algo for yuni", projs)
    assert hit is not None and hit["path"] == str(fake_home / "Desktop/yuni")


def test_match_task_normalizes_separators(fake_home):
    d = fake_home / "Desktop/my-app2"
    d.mkdir()
    (d / ".git").mkdir()
    projs = machine_index.projects(refresh=True)
    hit = machine_index.match_task("fix the bug in MyApp2", projs)
    assert hit is not None and hit["path"] == str(d)


def test_match_task_ambiguous_names_return_none(fake_home):
    projs = machine_index.projects(refresh=True)
    assert machine_index.match_task("compare yuni with dispatch", projs) is None


def test_match_task_duplicate_name_returns_none(fake_home):
    d = fake_home / "Documents/yuni"
    d.mkdir()
    (d / ".git").mkdir()
    projs = machine_index.projects(refresh=True)
    assert machine_index.match_task("look at yuni", projs) is None


def test_match_task_generic_names_never_pin(fake_home):
    d = fake_home / "Desktop/test"
    d.mkdir()
    (d / ".git").mkdir()
    projs = machine_index.projects(refresh=True)
    assert machine_index.match_task("run a quick test of the setup", projs) is None


def test_resolve_cwd_happy_path(fake_home):
    cwd = machine_index.resolve_cwd("explain the yuni matching algorithm", [])
    assert cwd == fake_home / "Desktop/yuni"


def test_resolve_cwd_respects_path_scope(fake_home):
    scoped = machine_index.resolve_cwd(
        "explain the yuni matching algorithm", [str(fake_home / "Documents")]
    )
    assert scoped is None
    in_scope = machine_index.resolve_cwd(
        "explain the yuni matching algorithm", [str(fake_home / "Desktop")]
    )
    assert in_scope == fake_home / "Desktop/yuni"


def test_resolve_cwd_skips_deleted_directory(fake_home):
    machine_index.projects(refresh=True)  # cache while it exists
    import shutil

    shutil.rmtree(fake_home / "Desktop/yuni")
    assert machine_index.resolve_cwd("explain yuni", []) is None


def test_index_prompt_lists_in_scope_projects(fake_home):
    machine_index.projects(refresh=True)
    prompt = machine_index.index_prompt([])
    assert prompt is not None
    assert str(fake_home / "Desktop/yuni") in prompt
    assert "NEVER search from the filesystem root" in prompt
    scoped = machine_index.index_prompt([str(fake_home / "Documents")])
    assert scoped is not None
    assert str(fake_home / "Documents/work/billing") in scoped
    assert str(fake_home / "Desktop/yuni") not in scoped


def test_index_prompt_none_when_no_projects(fake_home, monkeypatch):
    monkeypatch.setattr(machine_index, "projects", lambda refresh=False: [])
    assert machine_index.index_prompt([]) is None
