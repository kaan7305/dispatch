# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Dispatch tray app.

Build with:
    .venv/bin/pyinstaller Dispatch.spec
Output: dist/Dispatch.app
"""
import os
from pathlib import Path

SRC = Path("src")
WEB_DIR = SRC / "dispatch" / "web"
DESKTOP_DIST = WEB_DIR / "desktop" / "dist"

if not DESKTOP_DIST.exists():
    raise SystemExit(
        f"Desktop UI build not found at {DESKTOP_DIST}. "
        "Run `pnpm --dir src/dispatch/web/desktop build` first."
    )

block_cipher = None

a = Analysis(
    [str(SRC / "dispatch" / "tray" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        # Bundle the locally-served desktop SPA (React build output).
        # local_app.py mounts this as the static root on 127.0.0.1.
        (str(DESKTOP_DIST), "dispatch/web/desktop"),
    ],
    hiddenimports=[
        # rumps / PyObjC
        "rumps",
        "objc",
        "AppKit",
        "Foundation",
        "WebKit",
        "dispatch.tray.window",
        # FastAPI / uvicorn internals
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.loops.uvloop",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "fastapi",
        "starlette",
        "starlette.routing",
        "starlette.staticfiles",
        "starlette.middleware",
        # Crypto
        "cryptography",
        "jwt",
        "nacl",
        "nacl.signing",
        "nacl.encoding",
        # Keychain access
        "keyring",
        "keyring.backends",
        "keyring.backends.macOS",
        # Agent SDK
        "claude_agent_sdk",
        "mcp",
        # HTTP
        "httpx",
        "certifi",
        "websockets",
        "anyio",
        "anyio._backends._asyncio",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Dispatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Dispatch",
)

app = BUNDLE(
    coll,
    name="Dispatch.app",
    icon=None,
    bundle_identifier="com.dispatch.tray",
    info_plist={
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleName": "Dispatch",
        # Register dispatch:// so the broker /install page can deep-link
        # the installed app with the user's broker JWT after Clerk sign-in.
        "CFBundleURLTypes": [
            {
                "CFBundleURLName": "com.dispatch.tray",
                "CFBundleURLSchemes": ["dispatch"],
            }
        ],
    },
)
