"""Tests for the Codex host integration (``dispatch.codex``).

Two contracts are worth pinning here, and both are quiet-failure shaped — the
kind of bug that ships looking fine:

  1. **Discovery must translate, not just copy.** A Codex ``[mcp_servers.x]``
     table and a Claude ``.mcp.json`` entry describe the same two transports
     with different keys. Everything downstream (the executor, mcp_introspect,
     the permissions dialog) reads the Claude shape, so a passthrough would put
     entries in the pool that look present and fail to launch — the recipient
     grants a server that then never comes up.

  2. **Install must not eat the user's config.** ``config.toml`` is theirs; we
     append one table to it. A regression that rewrites the file, drops the
     surrounding tables, or double-appends on re-run is silent until they lose
     an MCP server they set up by hand.
"""
import pytest

from dispatch import codex


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Point $CODEX_HOME at a scratch dir so nothing reads or writes the real one."""
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def no_skill_source(monkeypatch):
    """Pretend this machine has no local copy of skills/dispatch/.

    find_skill_source() deliberately searches past $DISPATCH_SKILL_DIR into the
    source checkout and any installed Claude plugin bundle, so a test run from
    the repo always finds the real skill. Stubbing the lookup is the only way to
    exercise the branches where it comes up empty."""
    monkeypatch.setattr(codex, "find_skill_source", lambda: None)


def write_config(home, body: str):
    (home / "config.toml").write_text(body, encoding="utf-8")


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

def test_stdio_server_keeps_command_args_env(codex_home):
    write_config(codex_home, """
[mcp_servers.docs]
command = "docs-mcp"
args = ["--stdio", "--quiet"]
env = { DOCS_ROOT = "/srv/docs" }
startup_timeout_sec = 10
""")
    found = codex.discover_codex_mcp()
    assert found == {
        "docs": {
            "command": "docs-mcp",
            "args": ["--stdio", "--quiet"],
            "env": {"DOCS_ROOT": "/srv/docs"},
        }
    }
    # Codex-only knobs are dropped rather than passed through: the pool's
    # consumers speak the Claude entry shape and would ignore them anyway.
    assert "startup_timeout_sec" not in found["docs"]


def test_http_server_becomes_type_http(codex_home):
    write_config(codex_home, """
[mcp_servers.remote]
url = "https://example.test/mcp"
""")
    assert codex.discover_codex_mcp() == {
        "remote": {"type": "http", "url": "https://example.test/mcp"}
    }


def test_bearer_token_env_var_is_resolved_into_a_header(codex_home, monkeypatch):
    """Codex names an env var; the Claude shape wants a header. The value stays
    in-process — server configs never cross the local API boundary."""
    monkeypatch.setenv("REMOTE_TOKEN", "s3cret")
    write_config(codex_home, """
[mcp_servers.remote]
url = "https://example.test/mcp"
bearer_token_env_var = "REMOTE_TOKEN"
""")
    entry = codex.discover_codex_mcp()["remote"]
    assert entry["headers"] == {"Authorization": "Bearer s3cret"}


def test_unset_bearer_env_var_omits_the_header(codex_home, monkeypatch):
    """Better a handshake that fails with a real reason than one that sends
    'Bearer None' and gets a confusing 401."""
    monkeypatch.delenv("REMOTE_TOKEN", raising=False)
    write_config(codex_home, """
[mcp_servers.remote]
url = "https://example.test/mcp"
bearer_token_env_var = "REMOTE_TOKEN"
""")
    entry = codex.discover_codex_mcp()["remote"]
    assert "headers" not in entry
    assert entry["url"] == "https://example.test/mcp"


def test_disabled_and_transportless_servers_are_skipped(codex_home):
    write_config(codex_home, """
[mcp_servers.off]
command = "never-run"
enabled = false

[mcp_servers.empty]
description = "no command, no url"

[mcp_servers.live]
command = "run-me"
""")
    assert sorted(codex.discover_codex_mcp()) == ["live"]


def test_dispatch_is_never_discoverable(codex_home):
    """Handing a dispatch the Dispatch control plane would let task text
    re-wield the recipient's identity to dispatch onward."""
    write_config(codex_home, """
[mcp_servers.dispatch]
command = "dispatch-mcp"

[mcp_servers.other]
command = "other-mcp"
""")
    assert sorted(codex.discover_codex_mcp()) == ["other"]


def test_project_scoped_servers_are_merged_with_global_winning(codex_home, tmp_path):
    project = tmp_path / "proj"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "project-docs"\n'
        '[mcp_servers.extra]\ncommand = "extra-mcp"\n',
        encoding="utf-8",
    )
    write_config(codex_home, f"""
[mcp_servers.docs]
command = "global-docs"

[projects."{project.as_posix()}"]
trust_level = "trusted"
""")
    found = codex.discover_codex_mcp()
    assert found["docs"]["command"] == "global-docs"   # global wins on a clash
    assert found["extra"]["command"] == "extra-mcp"    # project-only still shows up


def test_malformed_config_yields_an_empty_pool(codex_home):
    """A broken host config costs the picker some rows, never an exception —
    discovery runs inside the daemon's dialog path."""
    write_config(codex_home, "[mcp_servers.broken\ncommand = ")
    assert codex.discover_codex_mcp() == {}


def test_missing_config_yields_an_empty_pool(codex_home):
    assert codex.discover_codex_mcp() == {}


# ----------------------------------------------------------------------------
# Install
# ----------------------------------------------------------------------------

def test_install_appends_and_preserves_existing_config(codex_home, no_skill_source):
    write_config(codex_home, """
model = "gpt-5"

[mcp_servers.docs]
command = "docs-mcp"
""")
    report = codex.install()
    assert report["mcp_server"] == "written"

    body = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-5"' in body          # untouched
    assert "[mcp_servers.docs]" in body       # their server survives
    entry = codex.server_entry()
    assert entry["command"] == "dispatch-mcp"
    # The two timeouts are load-bearing: the first launch spawns a daemon (up to
    # 30s) and one accept can supervise a run for 600s.
    assert entry["startup_timeout_sec"] >= 45
    assert entry["tool_timeout_sec"] >= 900


def test_install_into_a_machine_with_no_codex_config(codex_home, no_skill_source):
    report = codex.install()
    assert report["mcp_server"] == "written"
    assert codex.server_entry()["command"] == "dispatch-mcp"


def test_install_is_idempotent(codex_home, no_skill_source):
    codex.install()
    first = (codex_home / "config.toml").read_text(encoding="utf-8")
    report = codex.install()
    assert report["mcp_server"] == "already_present"
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == first


def test_force_replaces_the_block_and_keeps_a_backup(codex_home, no_skill_source):
    write_config(codex_home, """
[mcp_servers.dispatch]
command = "stale-path/dispatch-mcp"
startup_timeout_sec = 5

[mcp_servers.docs]
command = "docs-mcp"
""")
    report = codex.install(force=True)
    assert report["mcp_server"] == "replaced"

    entry = codex.server_entry()
    assert entry["command"] == "dispatch-mcp"
    assert entry["startup_timeout_sec"] >= 45
    # The table that followed ours must survive the rewrite.
    assert codex.discover_codex_mcp()["docs"]["command"] == "docs-mcp"
    assert (codex_home / "config.toml.bak").exists()
    assert "stale-path" in (codex_home / "config.toml.bak").read_text(encoding="utf-8")


def test_force_replace_stops_at_the_next_table(codex_home, no_skill_source):
    """The section rewrite must not swallow whatever follows it — including an
    array-of-tables header, which opens with '[[' rather than '['."""
    write_config(codex_home, """
[mcp_servers.dispatch]
command = "old"

[[profiles]]
name = "keep-me"
""")
    codex.install(force=True)
    body = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "[[profiles]]" in body
    assert 'name = "keep-me"' in body


def test_force_refuses_to_touch_an_inline_declaration(codex_home, no_skill_source):
    """A dispatch entry written inline under [mcp_servers] has no
    '[mcp_servers.dispatch]' header to replace. Appending our table anyway would
    duplicate the key and leave Codex unable to parse its own config at all — so
    refuse, and say where to look."""
    original = (
        '[mcp_servers]\n'
        'dispatch = { command = "dispatch-mcp" }\n'
        'docs = { command = "docs-mcp" }\n'
    )
    write_config(codex_home, original)

    report = codex.install(force=True)
    assert report["mcp_server"] == "manual_edit_needed"
    assert "by hand" in report["mcp_server_error"]
    # Untouched, and still parseable.
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == original
    assert codex.server_entry() is not None


def test_install_refuses_to_overwrite_an_unreadable_config(codex_home, no_skill_source,
                                                           monkeypatch):
    """Reading failing must not fall through to "treat it as empty" — that path
    would replace the user's whole config with our one block."""
    write_config(codex_home, 'model = "gpt-5"\n')

    def _boom(*a, **kw):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "not utf-8")

    monkeypatch.setattr(type(codex.config_path()), "read_text", _boom)
    with pytest.raises(OSError, match="refusing to overwrite"):
        codex.install()


def test_install_copies_the_skill_when_a_source_is_present(codex_home, tmp_path, monkeypatch):
    source = tmp_path / "skills" / "dispatch"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: dispatch\n---\nbody\n", encoding="utf-8")
    (source / "references" / "notes.md").write_text("notes\n", encoding="utf-8")
    monkeypatch.setenv("DISPATCH_SKILL_DIR", str(source))

    report = codex.install()
    assert report["skill"] == "copied"
    dest = codex_home / "skills" / "dispatch"
    assert (dest / "SKILL.md").read_text(encoding="utf-8").endswith("body\n")
    assert (dest / "references" / "notes.md").is_file()


def test_install_falls_back_to_supplied_skill_text(codex_home, no_skill_source):
    report = codex.install(skill_text="---\nname: dispatch\n---\nfetched\n")
    assert report["skill"] == "fetched"
    assert "fetched" in (codex_home / "skills" / "dispatch" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_install_without_a_skill_still_registers_the_server(codex_home, no_skill_source):
    """The skill only supplies natural-language triggers. Losing it must not
    cost the user the tools."""
    report = codex.install()
    assert report["skill"] == "skipped"
    assert report["skill_error"]
    assert codex.server_entry() is not None


def test_status_reports_wiring(codex_home, no_skill_source):
    before = codex.status()
    assert before["mcp_server_installed"] is False
    assert before["skill_installed"] is False

    codex.install(skill_text="---\nname: dispatch\n---\n")
    after = codex.status()
    assert after["mcp_server_installed"] is True
    assert after["mcp_server_command"] == "dispatch-mcp"
    assert after["skill_installed"] is True
