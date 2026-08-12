"""Browser-controller unit tests — the parts that don't need a live Chrome."""
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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


def test_chrome_binary_uses_the_shared_candidate_list(monkeypatch, tmp_path):
    """Discovery is `dispatch.desktop`'s list, not a second one kept here.

    The old private table listed neither Brave nor Chromium on Windows while
    the "no browser found" message named both, so `dispatch open` could put a
    window on screen a moment before this told the agent no browser existed.
    """
    brave = tmp_path / "brave.exe"
    brave.write_text("")
    monkeypatch.delenv("DISPATCH_BROWSER_BINARY", raising=False)
    monkeypatch.setattr(
        browser, "chromium_candidates",
        lambda: [tmp_path / "not-installed.exe", brave],
    )
    assert browser._chrome_binary() == str(brave)


def test_devtools_probe_ignores_a_configured_proxy(monkeypatch):
    """127.0.0.1:<debug port> must be reached directly.

    urlopen consults getproxies(), which on Windows reads HKCU Internet
    Settings — so on any machine with a system proxy the DevTools probe went
    to the proxy, came back as an error, and `_devtools_targets` returned
    None. Every command then reported browser control unavailable, including
    on a machine where the automation Chrome was up and answering.
    """
    payload = b'[{"type": "page", "url": "http://x/"}]'

    class Targets(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Targets)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(browser, "DEBUG_PORT", server.server_port)
        # Port 9 is discard: a proxied request can never come back from it.
        for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        assert browser._devtools_targets() == [{"type": "page", "url": "http://x/"}]
    finally:
        server.shutdown()
        server.server_close()


def test_the_loopback_opener_can_reach_nothing_but_the_url_it_is_given():
    # build_opener(ProxyHandler({})) suppresses the default ProxyHandler (the
    # one that calls getproxies()) and then drops the empty replacement too,
    # because a ProxyHandler holding no proxies registers no *_open methods.
    handlers = browser._DIRECT.handlers
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)
    assert any(isinstance(h, urllib.request.HTTPHandler) for h in handlers)


@pytest.mark.skipif(os.name != "nt", reason="reads this machine's real registry")
def test_windows_discovery_finds_a_real_browser():
    found = browser._chrome_binary()
    assert found is not None, "Windows 10+ always ships Edge"
    assert Path(found).exists()
