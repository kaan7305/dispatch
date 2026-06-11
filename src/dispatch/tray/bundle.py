"""Wrapper .app bundle so macOS treats the tray as a real application.

UNUserNotificationCenter only shows the permission prompt — and only
delivers banners — to processes whose executable lives inside a registered
app bundle. pipx runs the tray as a bare framework-Python process, so the
modern notification path silently fails (authorization stays notDetermined
forever) and we'd be stuck on the deprecated/osascript fallbacks.

ensure_bundle() builds a minimal ~/Applications/Dispatch.app:

    Contents/MacOS/Dispatch      — copy of the framework Python.app stub.
                                   Its install-name references are absolute
                                   and CPython derives sys.prefix from the
                                   linked libpython, so it runs from any
                                   location (verified: stdlib + ssl resolve).
    Contents/Resources/launch.py — adds this venv's site-packages and runs
                                   dispatch.tray.app.main().
    Contents/Info.plist          — com.dispatch.tray identity, LSUIElement,
                                   dispatch:// URL scheme.

Ad-hoc codesigned: TCC keys notification permission off the code identity,
and unsigned binaries get a fresh one every launch. Cheap to rebuild (one
binary copy + two small files), so callers refresh it on every enable().
"""
from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

BUNDLE_ID = "com.dispatch.tray"
APP_DIR = Path.home() / "Applications" / "Dispatch.app"

_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def _framework_python_app_binary() -> Path | None:
    """The GUI-capable Python.app stub of the framework this venv runs on."""
    cand = (
        Path(sys.base_prefix)
        / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    )
    return cand if cand.exists() else None


def ensure_bundle() -> Path | None:
    """Create or refresh the wrapper app. Returns the bundle executable,
    or None when there's nothing to wrap (non-framework / non-mac build)."""
    if sys.platform != "darwin":
        return None
    src_bin = _framework_python_app_binary()
    if src_bin is None:
        return None

    contents = APP_DIR / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    site_packages = sysconfig.get_paths()["purelib"]
    (resources / "launch.py").write_text(
        "import site\n"
        f"site.addsitedir({site_packages!r})\n"
        "from dispatch.tray.app import main\n"
        "main()\n"
    )

    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleName": "Dispatch",
                "CFBundleDisplayName": "Dispatch",
                "CFBundleExecutable": "Dispatch",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1.0",
                "CFBundleVersion": "1",
                # Menu-bar app: no Dock icon, no app switcher entry.
                "LSUIElement": True,
                "LSMinimumSystemVersion": "11.0",
                # dispatch://configure?... deep links from the broker install
                # page route here once LaunchServices knows the bundle.
                "CFBundleURLTypes": [
                    {
                        "CFBundleURLName": f"{BUNDLE_ID}.url",
                        "CFBundleURLSchemes": ["dispatch"],
                    }
                ],
            },
            fh,
        )

    dst_bin = macos / "Dispatch"
    if (
        not dst_bin.exists()
        or dst_bin.stat().st_size != src_bin.stat().st_size
        or dst_bin.stat().st_mtime < src_bin.stat().st_mtime
    ):
        shutil.copy2(src_bin, dst_bin)

    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(APP_DIR)],
        check=False, capture_output=True,
    )
    subprocess.run(
        [_LSREGISTER, "-f", str(APP_DIR)],
        check=False, capture_output=True,
    )
    return dst_bin


def bundle_program_arguments() -> list[str] | None:
    """argv for launchd/exec to start the tray through the bundle."""
    exe = ensure_bundle()
    if exe is None:
        return None
    return [str(exe), str(APP_DIR / "Contents" / "Resources" / "launch.py")]
