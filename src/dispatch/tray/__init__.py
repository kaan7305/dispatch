"""The always-on tray supervisor, per platform.

``dispatch-tray`` lands here rather than directly on a platform module, because
the two implementations cannot share an import: tray/app.py imports rumps and
AppKit at module scope, and tray/win_app.py imports pystray. Whichever one is
wrong for the current machine raises ImportError before its first line runs.

Before this router existed, ``dispatch-tray`` was declared as
``dispatch.tray.app:main`` on every platform, so on Windows pip installed a
launcher that could only ever crash on ``import objc`` — and the CLI's
``dispatch tray`` command spawned it, saw a process start, and reported
"look for ⬡ Dispatch in your menu bar".
"""
from __future__ import annotations

import sys


def main() -> int:
    """Run the tray for this platform."""
    if sys.platform == "darwin":
        from dispatch.tray.app import main as _main
    elif sys.platform == "win32":
        from dispatch.tray.win_app import main as _main
    else:
        sys.stderr.write(
            "dispatch-tray: there is no tray for this platform yet.\n"
            "Run `dispatch-daemon` directly, or `dispatch open` to start the "
            "daemon and open the UI.\n"
        )
        return 1
    result = _main()
    return 0 if result is None else int(result)
