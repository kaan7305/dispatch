"""Multi-broker config: legacy migration, upsert semantics, legacy mirroring.

The contract under test (shared/config.py): a pre-multi-broker config with
flat `broker`/`token` keys reads as a one-entry `brokers` list; every save
mirrors the FIRST entry back onto the flat keys so old code paths keep
working; `login --broker` adds/updates by URL instead of overwriting."""
import json

import pytest

from dispatch.shared import config as shared_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCH_HOME", str(tmp_path))
    monkeypatch.delenv("DISPATCH_BROKER", raising=False)
    monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
    return tmp_path


def _write(home, data):
    (home / "config.json").write_text(json.dumps(data))


def test_legacy_config_migrates_to_one_entry(home):
    _write(home, {"broker": "https://a.example/", "token": "jwt-a", "device_id": "dev-a"})
    entries = shared_config.broker_entries()
    assert entries == [
        {"url": "https://a.example", "token": "jwt-a", "label": "", "device_id": "dev-a"}
    ]


def test_empty_config_has_no_entries(home):
    assert shared_config.broker_entries() == []


def test_upsert_adds_second_broker_without_overwriting_first(home):
    _write(home, {"broker": "https://a.example", "token": "jwt-a", "device_id": "dev-a"})
    shared_config.upsert_broker("https://b.example/", token="jwt-b", label="work")
    entries = shared_config.broker_entries()
    assert [e["url"] for e in entries] == ["https://a.example", "https://b.example"]
    assert entries[0]["token"] == "jwt-a"
    assert entries[1] == {"url": "https://b.example", "token": "jwt-b",
                          "label": "work", "device_id": None}


def test_upsert_matches_by_url_and_updates_in_place(home):
    _write(home, {"brokers": [
        {"url": "https://a.example", "token": "old", "label": "home"},
        {"url": "https://b.example", "token": "jwt-b"},
    ]})
    shared_config.upsert_broker("https://a.example", token="new")
    entries = shared_config.broker_entries()
    assert len(entries) == 2
    assert entries[0]["token"] == "new"
    assert entries[0]["label"] == "home"   # None fields are preserved


def test_save_mirrors_first_entry_to_legacy_keys(home):
    shared_config.upsert_broker("https://a.example", token="jwt-a", device_id="dev-a")
    shared_config.upsert_broker("https://b.example", token="jwt-b")
    cfg = json.loads((home / "config.json").read_text())
    # Old code reading the flat keys sees the primary broker.
    assert cfg["broker"] == "https://a.example"
    assert cfg["token"] == "jwt-a"
    assert cfg["device_id"] == "dev-a"
    assert len(cfg["brokers"]) == 2


def test_legacy_token_wins_for_its_url(home):
    # Old code (pre-multi-broker sign-in) rewrote the flat token; the entry
    # for that URL must adopt it — the legacy keys are the fresher write.
    _write(home, {
        "broker": "https://a.example", "token": "fresh",
        "brokers": [{"url": "https://a.example", "token": "stale"}],
    })
    assert shared_config.broker_entries()[0]["token"] == "fresh"


def test_clear_broker_token_touches_only_that_broker(home):
    shared_config.upsert_broker("https://a.example", token="jwt-a")
    shared_config.upsert_broker("https://b.example", token="jwt-b")
    shared_config.clear_broker_token("https://a.example")
    entries = shared_config.broker_entries()
    assert entries[0]["token"] == ""
    assert entries[1]["token"] == "jwt-b"
    cfg = json.loads((home / "config.json").read_text())
    assert "token" not in cfg   # primary token cleared → legacy mirror cleared


def test_daemon_resolve_links_multi_home(home):
    from argparse import Namespace
    from dispatch.daemon.main import _resolve_links

    shared_config.upsert_broker("https://a.example", token="jwt-a")
    shared_config.upsert_broker("https://b.example", token="jwt-b", label="work")
    shared_config.upsert_broker("https://c.example", token=None)  # signed out
    cfg = shared_config.load_config()

    links = _resolve_links(Namespace(broker=None, token=None), cfg)
    # One link per SIGNED-IN broker; signed-out entries hold no connection.
    assert [(l.url, l.token) for l in links] == [
        ("https://a.example", "jwt-a"), ("https://b.example", "jwt-b"),
    ]
    assert links[1].label == "work"


def test_daemon_resolve_links_explicit_broker_pins_single(home):
    from argparse import Namespace
    from dispatch.daemon.main import _resolve_links

    shared_config.upsert_broker("https://a.example", token="jwt-a")
    shared_config.upsert_broker("https://b.example", token="jwt-b")
    cfg = shared_config.load_config()

    links = _resolve_links(Namespace(broker="https://b.example", token=None), cfg)
    assert [(l.url, l.token) for l in links] == [("https://b.example", "jwt-b")]
