"""machine_index: the daemon-side resolver that starts a delegated agent in
the right directory on any machine — scan, cache, deterministic matching,
and scope filtering."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dispatch.daemon import machine_index

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows filesystem semantics"
)


def _set_hidden(p: Path) -> None:
    """Mark a directory hidden the way Windows means it — an attribute bit,
    not a leading dot."""
    import ctypes

    FILE_ATTRIBUTE_HIDDEN = 0x2
    if not ctypes.windll.kernel32.SetFileAttributesW(
        str(p), FILE_ATTRIBUTE_HIDDEN
    ):
        raise OSError(f"SetFileAttributesW failed on {p}")


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


def test_onedrive_is_skipped_only_where_it_is_a_mirror():
    # Windows 11 puts Desktop/Documents *inside* OneDrive by default, so the
    # skip that is correct on macOS empties the index there.
    assert ("OneDrive" in machine_index._SKIP_DIRS) == (sys.platform != "win32")


def test_onedrive_kfm_tree_is_indexed_on_windows(fake_home):
    """Known Folder Move layout: the only copy of the user's work is under
    ~/OneDrive, and before this it was skipped wholesale."""
    proj = fake_home / "OneDrive/Desktop/kfmproj"
    (proj / ".git").mkdir(parents=True)
    paths = {p["path"] for p in machine_index._scan()}
    if sys.platform == "win32":
        assert str(proj) in paths
    else:
        assert str(proj) not in paths


def test_resolve_cwd_pins_a_kfm_project(fake_home):
    proj = fake_home / "OneDrive/Desktop/kfmproj"
    (proj / ".git").mkdir(parents=True)
    machine_index.projects(refresh=True)
    expected = proj if sys.platform == "win32" else None
    assert machine_index.resolve_cwd("review the kfmproj tests", []) == expected


@windows_only
def test_scan_skips_hidden_directories(fake_home):
    hidden = fake_home / "Desktop/hiddenproj"
    (hidden / ".git").mkdir(parents=True)
    _set_hidden(hidden)
    paths = {p["path"] for p in machine_index._scan()}
    assert str(hidden) not in paths
    assert str(fake_home / "Desktop/yuni") in paths  # the walk still ran


@windows_only
def test_scan_skips_windows_profile_noise(fake_home):
    noise = fake_home / "3D Objects/model"
    (noise / ".git").mkdir(parents=True)
    assert str(noise) not in {p["path"] for p in machine_index._scan()}


@windows_only
def test_junction_does_not_index_a_project_twice(fake_home):
    """A junction is a reparse point that is_symlink() does not report, so the
    tree behind it used to be indexed again under the alias path — and a name
    found in two places is a name match_task refuses to pin."""
    link = fake_home / "mirror"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(fake_home / "Desktop")],
        capture_output=True, text=True, check=True,
    )
    paths = {p["path"] for p in machine_index._scan()}
    assert str(fake_home / "Desktop/yuni") in paths
    assert str(link / "yuni") not in paths
    assert machine_index.match_task(
        "explain yuni", machine_index._scan()
    ) is not None


@windows_only
def test_a_junction_that_is_the_only_route_in_is_still_indexed(tmp_path, monkeypatch):
    """Skipping every reparse point would fix the double-index above by making
    junctions invisible — which breaks the ordinary case of keeping projects on
    a second drive and junctioning them into the profile. Then the tree cannot
    be found at all, which is the failure this whole module exists to prevent.

    Deduping on each directory's *resolved* identity handles both: an alias is
    recognised as somewhere already visited, while a junction that is the only
    way in is walked exactly once.
    """
    home = tmp_path / "home"
    external = tmp_path / "external"
    home.mkdir()
    (external / "Projects" / "acme" / ".git").mkdir(parents=True)

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(home / "Projects"), str(external / "Projects")],
        capture_output=True, text=True, check=True,
    )
    monkeypatch.setattr(machine_index, "_scan_roots", lambda: [home])

    names = {p["name"] for p in machine_index._scan()}
    assert "acme" in names, "a project reachable only through a junction was skipped"


@windows_only
def test_redirected_known_folder_is_seeded_as_its_own_root(fake_home, monkeypatch):
    """A KFM'd Desktop sits one level deeper than ~/Desktop, so walking only
    from home reaches one level less of it. Seeding it back restores the depth
    the same layout gets on macOS."""
    desktop = fake_home / "OneDrive - Contoso/Desktop"
    nested = desktop / "work/deepproj"
    (nested / ".git").mkdir(parents=True)
    assert str(nested) not in {p["path"] for p in machine_index._scan()}

    monkeypatch.setattr(
        machine_index, "_known_folder",
        lambda fid: desktop if fid == machine_index._FOLDERID_DESKTOP else None,
    )
    assert machine_index._scan_roots() == [desktop, fake_home]
    paths = [p["path"] for p in machine_index._scan()]
    assert str(nested) in paths
    # Seeding must not double-count what home's walk reaches as well.
    assert len(paths) == len(set(paths))


@windows_only
def test_known_folder_lookup_matches_the_live_profile():
    """The ctypes call itself, against the real shell — a wrong GUID or a
    leaked CoTaskMem buffer would show up here and nowhere else."""
    desktop = machine_index._known_folder(machine_index._FOLDERID_DESKTOP)
    docs = machine_index._known_folder(machine_index._FOLDERID_DOCUMENTS)
    assert desktop is not None and desktop.is_dir()
    assert docs is not None and docs.is_dir()
    # Only the *display* name of a known folder is localized; the directory on
    # disk is "Desktop" on every language build, so a swapped GUID shows here.
    assert desktop.name.lower() == "desktop"
    assert machine_index._known_folder("{not-a-guid}") is None


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
