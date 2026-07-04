#!/usr/bin/env python3
"""Vendor a self-contained agent runtime into ./vendor for bundling.

Downloads a portable Node.js and installs the Claude Code + Codex CLIs into a
relocatable tree so the packaged Dispatch.app can run agents with zero terminal
setup on the recipient's machine. Run this on the BUILD machine before
PyInstaller; `Dispatch.spec` bundles the resulting `vendor/` into the .app.

    python scripts/vendor_agents.py                 # host arch
    python scripts/vendor_agents.py --arch arm64    # or x64
    python scripts/vendor_agents.py --dest ~/Library/Application\\ Support/Dispatch/vendor

Resulting layout (matches dispatch.executor.runtime.find_runtime):

    vendor/
      bin/    node, claude, codex     (this dir goes first on PATH at runtime)
      lib/    node_modules/...
      NOTICE  third-party license pointers

⚠️  LICENSING — READ BEFORE REDISTRIBUTING
    Codex CLI (@openai/codex) is Apache-2.0: redistribution is fine with
    attribution (kept in NOTICE). Claude Code (@anthropic-ai/claude-code) is
    Anthropic's PROPRIETARY software; confirm Anthropic permits redistributing
    it inside your installer before you ship a bundled build publicly. If they
    don't, run this script with --dest pointing at the per-user Application
    Support dir from a first-launch step instead of bundling it (same code
    path — runtime.py finds it there too), so the user's own machine fetches it
    from npm rather than you redistributing it.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Pinned so builds are reproducible. Bump deliberately.
NODE_VERSION = "22.11.0"          # current LTS ("Jod")
CLAUDE_PKG = "@anthropic-ai/claude-code"
CODEX_PKG = "@openai/codex"

REPO_ROOT = Path(__file__).resolve().parents[1]


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    raise SystemExit(f"unsupported arch: {platform.machine()}")


def _node_url(arch: str) -> str:
    # macOS only for now; extend for linux/win when those daemons ship.
    if sys.platform != "darwin":
        raise SystemExit(
            "vendor_agents.py currently targets macOS; the .app is Mac-only."
        )
    return (
        f"https://nodejs.org/dist/v{NODE_VERSION}/"
        f"node-v{NODE_VERSION}-darwin-{arch}.tar.gz"
    )


def _download(url: str, dest: Path) -> None:
    print(f"  ↓ {url}")
    with urllib.request.urlopen(url) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def _stage_node(vendor: Path, arch: str) -> Path:
    """Download + extract a portable Node into vendor/. Returns the node binary."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tgz = tmp / "node.tar.gz"
        _download(_node_url(arch), tgz)
        with tarfile.open(tgz) as t:
            t.extractall(tmp)
        extracted = next(tmp.glob(f"node-v{NODE_VERSION}-darwin-{arch}"))
        # Copy node + its bundled npm into the vendor tree.
        (vendor / "bin").mkdir(parents=True, exist_ok=True)
        (vendor / "lib").mkdir(parents=True, exist_ok=True)
        shutil.copy2(extracted / "bin" / "node", vendor / "bin" / "node")
        shutil.copytree(
            extracted / "lib" / "node_modules" / "npm",
            vendor / "lib" / "node_modules" / "npm",
            dirs_exist_ok=True,
        )
        # npm's own bin shims (npm, npx).
        for shim in ("npm", "npx"):
            src = extracted / "bin" / shim
            if src.exists():
                shutil.copy2(src, vendor / "bin" / shim, follow_symlinks=True)
    node = vendor / "bin" / "node"
    node.chmod(0o755)
    return node


def _install_clis(vendor: Path, node: Path) -> None:
    """Use the vendored npm to install the CLIs with --prefix=vendor, so their
    bin shims land in vendor/bin and their code in vendor/lib/node_modules."""
    npm_cli = vendor / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    env = dict(os.environ)
    env["PATH"] = f"{vendor / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    cmd = [
        str(node), str(npm_cli), "install",
        "--global",                       # honors --prefix for layout
        "--prefix", str(vendor),
        "--no-audit", "--no-fund",
        CLAUDE_PKG, CODEX_PKG,
    ]
    print("  ⚙  installing", CLAUDE_PKG, "+", CODEX_PKG)
    subprocess.run(cmd, env=env, check=True)


def _write_notice(vendor: Path) -> None:
    (vendor / "NOTICE").write_text(
        "Dispatch bundled agent runtime\n"
        "==============================\n\n"
        f"node            v{NODE_VERSION}   (MIT / OpenJS Foundation)\n"
        f"{CODEX_PKG}      Apache-2.0 (OpenAI)\n"
        f"{CLAUDE_PKG}     proprietary (Anthropic) — see licensing note in\n"
        "                 scripts/vendor_agents.py before redistributing.\n\n"
        "Full license texts ship inside each package under\n"
        "vendor/lib/node_modules/<pkg>/.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arch", choices=["arm64", "x64"], default=_arch())
    ap.add_argument("--dest", type=Path, default=REPO_ROOT / "vendor")
    ap.add_argument("--clean", action="store_true", help="wipe dest first")
    args = ap.parse_args()

    vendor = args.dest.expanduser().resolve()
    if args.clean and vendor.exists():
        shutil.rmtree(vendor)
    vendor.mkdir(parents=True, exist_ok=True)

    print(f"Vendoring agent runtime → {vendor}  (darwin-{args.arch})")
    node = _stage_node(vendor, args.arch)
    _install_clis(vendor, node)
    _write_notice(vendor)

    # Sanity: the two things runtime.py insists on.
    ok = (vendor / "bin" / "node").exists() and (vendor / "bin" / "claude").exists()
    print()
    for name in ("node", "claude", "codex"):
        p = vendor / "bin" / name
        print(f"  {'✓' if p.exists() else '✗'} vendor/bin/{name}")
    if not ok:
        print("\nERROR: node or claude shim missing — install failed.", file=sys.stderr)
        return 1
    print("\nDone. `Dispatch.spec` will bundle this into the .app.")
    print("Reminder: sign every binary under vendor/ during notarization "
          "(see docs/BUNDLING.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
