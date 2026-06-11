"""Cross-platform browser control for dispatched agents.

A dispatched agent that holds `Bash` can drive a real Chrome/Chromium
through this CLI — open pages, run JS, click, type, control video, take
screenshots — on any machine Dispatch is installed on. It speaks the
Chrome DevTools Protocol (the same wire Puppeteer/Playwright use), so it
needs no extra pip packages (only `websockets`, already a core dep) and
no macOS Automation/Accessibility grants.

Why CDP and not AppleScript: Chrome disables "execute JavaScript through
AppleScript" by default and the toggle is only reachable from Chrome's
own menu, so AppleScript can open URLs but not control the page. CDP has
no such gate and is identical across macOS / Linux / Windows.

A dedicated automation profile + debugging port is launched on first use
and reused after (the handle is cached in a temp file), so repeated
commands in one task hit the same window. This profile is separate from
the user's everyday Chrome — it is NOT signed into their accounts, which
is the right default for a delegated task touching someone else's machine.

Usage (the agent calls these via Bash):

    python -m dispatch.browser open <url>
    python -m dispatch.browser eval '<javascript>'      # prints JSON result
    python -m dispatch.browser eval -f script.js
    python -m dispatch.browser click '<css-selector>'
    python -m dispatch.browser type '<css-selector>' '<text>'
    python -m dispatch.browser video play|pause|toggle
    python -m dispatch.browser video seek <fraction 0..1>
    python -m dispatch.browser status                   # url, title, video state
    python -m dispatch.browser screenshot <path.png>
    python -m dispatch.browser text [<css-selector>]    # visible text
    python -m dispatch.browser close

All commands act on the active tab of the automation window. Every
command prints a one-line JSON object to stdout; a non-zero exit means
the JSON has an "error" field.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

DEBUG_PORT = int(os.environ.get("DISPATCH_BROWSER_PORT", "9333"))
PROFILE_DIR = Path(tempfile.gettempdir()) / "dispatch-browser-profile"
_READY_TIMEOUT_S = 20.0


# ── Chrome discovery ────────────────────────────────────────────────────

def _chrome_binary() -> str | None:
    """Absolute path to a Chrome/Chromium/Edge binary, or None."""
    env = os.environ.get("DISPATCH_BROWSER_BINARY")
    if env and Path(env).exists():
        return env

    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif sys.platform.startswith("win"):
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pfx86}\Google\Chrome\Application\chrome.exe",
            rf"{local}\Google\Chrome\Application\chrome.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{pfx86}\Microsoft\Edge\Application\msedge.exe",
        ]
    else:  # linux / other unix
        names = [
            "google-chrome", "google-chrome-stable", "chromium",
            "chromium-browser", "brave-browser", "microsoft-edge",
        ]
        found = [shutil.which(n) for n in names]
        candidates = [p for p in found if p]
        candidates += [
            "/usr/bin/google-chrome", "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]

    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


# ── DevTools endpoint ───────────────────────────────────────────────────

def _devtools_targets() -> list[dict] | None:
    try:
        raw = urllib.request.urlopen(
            f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=1.0
        ).read()
        return json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _ensure_browser(initial_url: str | None = None) -> dict:
    """Return a page target dict, launching Chrome with remote debugging if
    it isn't already up. Raises RuntimeError with a clear message on failure."""
    targets = _devtools_targets()
    if targets is None:
        binary = _chrome_binary()
        if binary is None:
            raise RuntimeError(
                "no Chrome/Chromium/Edge/Brave found. Install Google Chrome, "
                "or set DISPATCH_BROWSER_BINARY to a Chromium-based browser."
            )
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = [
            binary,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if initial_url:
            args.append(initial_url)
        # Detach so the browser outlives this short-lived CLI invocation.
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)

        deadline = time.time() + _READY_TIMEOUT_S
        while time.time() < deadline:
            targets = _devtools_targets()
            if targets:
                break
            time.sleep(0.25)
        if not targets:
            raise RuntimeError(
                f"Chrome did not expose the DevTools port {DEBUG_PORT} within "
                f"{_READY_TIMEOUT_S:.0f}s."
            )

    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("browser is up but has no page target.")
    # Prefer a real http(s) page over the new-tab page.
    real = [p for p in pages if p.get("url", "").startswith("http")]
    return (real or pages)[0]


# ── CDP command plumbing ────────────────────────────────────────────────

async def _cdp(ws_url: str, method: str, params: dict | None = None) -> dict:
    async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "CDP error"))
                return msg.get("result", {})


async def _eval(ws_url: str, expression: str) -> object:
    """Evaluate JS in the page, awaiting promises, returning a JSON value."""
    result = await _cdp(ws_url, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
        "userGesture": True,
    })
    if result.get("exceptionDetails"):
        exc = result["exceptionDetails"]
        msg = exc.get("exception", {}).get("description") or exc.get("text", "JS error")
        raise RuntimeError(msg)
    return result.get("result", {}).get("value")


# JS snippets reused across commands. document.querySelector('video') finds the
# primary <video> on the page (YouTube, Vimeo, most players).
_VIDEO_STATE_JS = """
(() => {
  const v = document.querySelector('video');
  if (!v) return JSON.stringify({video: null});
  return JSON.stringify({video: {
    state: v.paused ? 'paused' : 'playing',
    at: Math.round(v.currentTime),
    duration: isFinite(v.duration) ? Math.round(v.duration) : null,
  }});
})()
"""


# ── Commands ────────────────────────────────────────────────────────────

async def cmd_open(url: str) -> dict:
    page = _ensure_browser(initial_url=url)
    ws_url = page["webSocketDebuggerUrl"]
    await _cdp(ws_url, "Page.enable")
    await _cdp(ws_url, "Page.navigate", {"url": url})
    # Give the document a moment to start loading before reporting.
    await asyncio.sleep(1.0)
    title = await _eval(ws_url, "document.title")
    return {"ok": True, "url": url, "title": title}


async def cmd_eval(expression: str) -> dict:
    page = _ensure_browser()
    value = await _eval(page["webSocketDebuggerUrl"], expression)
    return {"ok": True, "result": value}


async def cmd_click(selector: str) -> dict:
    page = _ensure_browser()
    js = (
        f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
        f"if (!el) return 'not found'; el.scrollIntoView({{block:'center'}}); "
        f"el.click(); return 'clicked'; }})()"
    )
    res = await _eval(page["webSocketDebuggerUrl"], js)
    if res != "clicked":
        raise RuntimeError(f"selector {selector!r} {res}")
    return {"ok": True, "clicked": selector}


async def cmd_type(selector: str, text: str) -> dict:
    page = _ensure_browser()
    js = (
        f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
        f"if (!el) return 'not found'; el.focus(); "
        f"el.value = {json.dumps(text)}; "
        f"el.dispatchEvent(new Event('input', {{bubbles:true}})); "
        f"el.dispatchEvent(new Event('change', {{bubbles:true}})); "
        f"return 'typed'; }})()"
    )
    res = await _eval(page["webSocketDebuggerUrl"], js)
    if res != "typed":
        raise RuntimeError(f"selector {selector!r} {res}")
    return {"ok": True, "typed_into": selector}


async def cmd_video(action: str, fraction: float | None) -> dict:
    page = _ensure_browser()
    ws_url = page["webSocketDebuggerUrl"]
    if action == "play":
        js = "(()=>{const v=document.querySelector('video');if(!v)return 'no video';v.play();return 'ok';})()"
    elif action == "pause":
        js = "(()=>{const v=document.querySelector('video');if(!v)return 'no video';v.pause();return 'ok';})()"
    elif action == "toggle":
        js = "(()=>{const v=document.querySelector('video');if(!v)return 'no video';v.paused?v.play():v.pause();return 'ok';})()"
    elif action == "seek":
        frac = max(0.0, min(1.0, fraction if fraction is not None else 0.0))
        js = (
            f"(()=>{{const v=document.querySelector('video');if(!v)return 'no video';"
            f"v.currentTime=v.duration*{frac};return 'ok';}})()"
        )
    else:
        raise RuntimeError(f"unknown video action {action!r}")
    res = await _eval(ws_url, js)
    if res == "no video":
        raise RuntimeError("no <video> element on the page")
    state = json.loads(await _eval(ws_url, _VIDEO_STATE_JS))
    return {"ok": True, "action": action, **state}


async def cmd_status() -> dict:
    page = _ensure_browser()
    ws_url = page["webSocketDebuggerUrl"]
    url = await _eval(ws_url, "location.href")
    title = await _eval(ws_url, "document.title")
    state = json.loads(await _eval(ws_url, _VIDEO_STATE_JS))
    return {"ok": True, "url": url, "title": title, **state}


async def cmd_text(selector: str | None) -> dict:
    page = _ensure_browser()
    target = f"document.querySelector({json.dumps(selector)})" if selector else "document.body"
    js = f"(() => {{ const el = {target}; return el ? el.innerText.slice(0, 20000) : null; }})()"
    value = await _eval(page["webSocketDebuggerUrl"], js)
    if value is None:
        raise RuntimeError(f"selector {selector!r} not found")
    return {"ok": True, "text": value}


async def cmd_screenshot(path: str) -> dict:
    import base64
    page = _ensure_browser()
    res = await _cdp(page["webSocketDebuggerUrl"], "Page.captureScreenshot", {"format": "png"})
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(res["data"]))
    return {"ok": True, "screenshot": str(out)}


async def cmd_close() -> dict:
    targets = _devtools_targets()
    if targets is None:
        return {"ok": True, "note": "no automation browser running"}
    for t in targets:
        if t.get("type") == "page":
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{DEBUG_PORT}/json/close/{t['id']}", timeout=1.0
                )
            except OSError:
                pass
    return {"ok": True, "closed": True}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dispatch-browser", description="Control a real browser over CDP.")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open"); o.add_argument("url")
    e = sub.add_parser("eval")
    e.add_argument("expression", nargs="?", default=None)
    e.add_argument("-f", "--file", help="read JS from a file instead")
    c = sub.add_parser("click"); c.add_argument("selector")
    t = sub.add_parser("type"); t.add_argument("selector"); t.add_argument("text")
    v = sub.add_parser("video"); v.add_argument("action", choices=["play", "pause", "toggle", "seek"])
    v.add_argument("fraction", nargs="?", type=float, default=None)
    sub.add_parser("status")
    tx = sub.add_parser("text"); tx.add_argument("selector", nargs="?", default=None)
    s = sub.add_parser("screenshot"); s.add_argument("path")
    sub.add_parser("close")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "open":
            result = asyncio.run(cmd_open(args.url))
        elif args.cmd == "eval":
            expr = args.expression
            if args.file:
                expr = Path(args.file).read_text()
            if not expr:
                raise RuntimeError("eval needs an expression or -f FILE")
            result = asyncio.run(cmd_eval(expr))
        elif args.cmd == "click":
            result = asyncio.run(cmd_click(args.selector))
        elif args.cmd == "type":
            result = asyncio.run(cmd_type(args.selector, args.text))
        elif args.cmd == "video":
            result = asyncio.run(cmd_video(args.action, args.fraction))
        elif args.cmd == "status":
            result = asyncio.run(cmd_status())
        elif args.cmd == "text":
            result = asyncio.run(cmd_text(args.selector))
        elif args.cmd == "screenshot":
            result = asyncio.run(cmd_screenshot(args.path))
        elif args.cmd == "close":
            result = asyncio.run(cmd_close())
        else:  # unreachable — argparse enforces choices
            raise RuntimeError(f"unknown command {args.cmd!r}")
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
