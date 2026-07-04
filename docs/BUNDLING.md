# Bundling: one download, no terminal

Goal: a non-technical person clicks **Download**, opens the app, and it runs
dispatches with a local AI agent — without ever touching a terminal, npm, or an
API key by hand.

The Claude Agent SDK doesn't call a model API directly; it shells out to the
`claude` CLI, which is a Node.js program (Codex is the same). So "install the
app" really means shipping **three** things together: the Dispatch app, a Node
runtime, and the `claude` + `codex` CLIs — plus a way to authenticate that
doesn't require a terminal.

## The pieces

| Piece | How it ships | Where it lives at runtime |
|---|---|---|
| Dispatch app (Python + tray + daemon + web UI) | PyInstaller → `Dispatch.app` | the `.app` |
| Node.js + `claude` + `codex` | `scripts/vendor_agents.py` → `vendor/`, bundled by `Dispatch.spec` | `.app/Contents/Resources/vendor/bin` (put on `PATH` by `dispatch.executor.runtime`) |
| Credentials | first-run screen in the app | keychain / `~/.dispatch/config.json` (key) or `~/.claude.json` (subscription login) |

At runtime, `dispatch.executor.runtime.prepare_agent_runtime()` finds the
vendored `vendor/bin` and prepends it to `PATH`, so the SDK's
`which("claude")` resolves to our copy and that copy's `#!/usr/bin/env node`
resolves to our Node. It's called from both the daemon startup and defensively
in `run_dispatch`. Nothing is global; nothing needs a terminal.

## Build steps

```bash
# 1. Build the web UI the app serves locally
pnpm --dir src/dispatch/web/desktop build

# 2. Vendor the agent runtime (Node + both CLIs) into ./vendor
python scripts/vendor_agents.py            # add --arch x64 for Intel builds

# 3. Build the .app
.venv/bin/pyinstaller Dispatch.spec

# 4. Sign every nested binary, notarize, staple, and make a DMG
export DISPATCH_SIGN_ID="Developer ID Application: Your Name (TEAMID)"
./scripts/sign_and_notarize.sh
```

Ship `dist/Dispatch.dmg`.

## Authentication (the hybrid flow)

Two paths, both terminal-free, both already supported by the daemon:

1. **Sign in with a subscription** (default for non-tech users). The first-run
   screen launches the vendored CLI's browser login (`claude` writes
   `~/.claude.json`; Codex writes its config dir). The daemon already honors
   `CLAUDE_CONFIG_PATH` / `~/.claude.json`, so no key is needed. Requires the
   recipient to have a paid Claude / ChatGPT plan.
2. **Paste an API key**. The first-run screen writes it to
   `~/.dispatch/config.json` (`anthropic_api_key`, mode 0600); the daemon
   exports it as `ANTHROPIC_API_KEY`. Good for power users.

The daemon only warns about missing credentials when *neither* a key nor a
login file is present, so subscription users don't get a false alarm.

> TODO (UI): the first-run "Connect your AI" screen in `web/desktop` should
> offer both buttons. The daemon plumbing (key → config, login → `~/.claude.json`)
> is done; this is the remaining front-end work.

## Apple credentials you need (one-time)

- **Apple Developer Program** membership ($99/yr) → a *Developer ID Application*
  certificate in your login keychain.
- A **notarytool keychain profile** so the script can submit without a password
  in the clear:
  ```bash
  xcrun notarytool store-credentials dispatch-notary \
    --apple-id you@example.com --team-id TEAMID --password <app-specific-password>
  ```
  (Generate the app-specific password at appleid.apple.com.)

Without notarization, Gatekeeper shows "unidentified developer — cannot be
opened" and a non-technical user is stuck. There is no terminal-free way around
this; it is the price of the one-click experience.

## ⚠️ Licensing — confirm before public release

- **Codex CLI** (`@openai/codex`) is Apache-2.0 — bundling and redistributing is
  fine; attribution is kept in `vendor/NOTICE`.
- **Claude Code** (`@anthropic-ai/claude-code`) is **Anthropic proprietary**.
  Redistributing it inside your own installer may require Anthropic's
  permission. If it isn't permitted, don't vendor it at build time — instead run
  `scripts/vendor_agents.py --dest "~/Library/Application Support/Dispatch/vendor"`
  from a **first-launch** step so the user's own machine fetches it from npm.
  `runtime.py` already looks in that Application Support path, so the app code
  doesn't change — only *where* the fetch happens does. This keeps you on the
  right side of redistribution while preserving the no-terminal experience.

## Transparency (a product requirement, not just legal)

Dispatch's whole pitch is informed consent. Ship a short first-run screen that
says, in plain words, *"Dispatch runs an AI agent on your Mac. It only ever acts
on tasks you approve, and every risky command asks you first."* Same one click —
but the user understands what they installed, which is the honest version of
"they don't have to deal with the terminal."
