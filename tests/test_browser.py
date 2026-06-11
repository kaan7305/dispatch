"""Browser-controller unit tests — the parts that don't need a live Chrome."""
import json
import sys

import pytest

from dispatch import browser


def test_parser_accepts_all_commands():
    p = browser._build_parser()
    assert p.parse_args(["open", "https://x.com"]).url == "https://x.com"
    assert p.parse_args(["video", "seek", "0.5"]).fraction == 0.5
    assert p.parse_args(["video", "play"]).fraction is None
    assert p.parse_args(["click", "#btn"]).selector == "#btn"
    assert p.parse_args(["type", "#q", "hi"]).text == "hi"
    assert p.parse_args(["text"]).selector is None
    assert p.parse_args(["screenshot", "/tmp/a.png"]).path == "/tmp/a.png"


def test_video_seek_clamps_fraction(monkeypatch):
    """seek must clamp out-of-range fractions into [0,1] before building JS."""
    captured = {}

    async def fake_eval(ws_url, expr):
        captured.setdefault("exprs", []).append(expr)
        # The seek assigns currentTime; the state read only reads it.
        if "currentTime=" in expr.replace(" ", ""):
            return "ok"
        return json.dumps({"video": {"state": "playing", "at": 0, "duration": 10}})

    monkeypatch.setattr(browser, "_ensure_browser", lambda *a, **k: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(browser, "_eval", fake_eval)

    import asyncio
    asyncio.run(browser.cmd_video("seek", 5.0))   # 5.0 → clamp to 1.0
    seek_expr = next(e for e in captured["exprs"] if "currentTime" in e)
    assert "*1.0" in seek_expr   # clamped, not *5.0


def test_main_prints_error_json_on_failure(capsys, monkeypatch):
    """A failing command exits non-zero with an {"error": ...} JSON line."""
    monkeypatch.setattr(
        browser, "cmd_status",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # cmd_status is async; patch asyncio.run to surface the error path simply.
    monkeypatch.setattr(browser.asyncio, "run", lambda coro: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = browser.main(["status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["error"] == "boom"


def test_chrome_binary_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setenv("DISPATCH_BROWSER_BINARY", str(fake))
    assert browser._chrome_binary() == str(fake)


def test_chrome_binary_missing_env_falls_through(monkeypatch):
    monkeypatch.setenv("DISPATCH_BROWSER_BINARY", "/nonexistent/chrome")
    # Should not return the bogus path; falls back to platform discovery (which
    # may be None in CI — either way, never the missing override).
    assert browser._chrome_binary() != "/nonexistent/chrome"
