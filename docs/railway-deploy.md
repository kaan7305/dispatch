# Deploying the broker to Railway

The broker (`src/dispatch/broker/app.py`) is a FastAPI + WebSocket relay
service. This doc is the exact click-by-click path to get it live on
Railway, plus the two most likely causes if you're seeing **"There is no
active deployment for this service"** or **"There was an error deploying
from source"** with no build logs.

## Root cause ranking (do these in order)

An **instant** failure with **zero build logs** almost always means Railway
never got as far as running a builder at all — it's an account or
repo-access problem, not a code problem. Check these first, in order:

### 1. Free/Trial account "Limited Trial" restriction (most likely)

Railway silently puts new/thin-history GitHub accounts on a **Limited
Trial**. Limited Trial accounts can deploy a *database* but are blocked
from deploying an *application from a GitHub source* entirely — which
produces exactly this symptom (instant failure, generic message, no logs).

**Check:** In the Railway dashboard, look for a "Limited Trial" / trial
restriction banner on the project or account settings page. Try creating a
brand-new empty project and deploying any public throwaway repo — if that
also fails instantly with no logs, it's account-level, not this repo.

**Fix, in order of effort:**
1. Visit `railway.com/verify` and (re)link/verify the GitHub account tied
   to your Railway login.
2. If that doesn't lift it, email `billing@railway.app` from your
   Railway-registered address explaining the use case — Railway has
   granted manual exceptions on request.
3. Upgrade to the **Hobby plan** ($5/mo) — this bypasses the Limited Trial
   restriction outright and is the guaranteed fix.

### 2. Railway's GitHub App doesn't have access to `kaan7305/dispatch`

Connecting a repo requires the **Railway GitHub App** to be installed and
explicitly granted access to that specific repository — installing the app
on your own account does *not* automatically grant it access to repos you
don't own, and org-owned repos need an org admin to approve the
installation/grant.

**Check (as the GitHub user Railway is connected to):**
1. GitHub → your avatar → **Settings** → **Applications** →
   **Installed GitHub Apps** (or `github.com/settings/installations`).
2. Find **Railway**, click **Configure**.
3. Under "Repository access," confirm `kaan7305/dispatch` is listed
   (either via "All repositories" or explicitly selected). If it's
   missing, add it. If the button says "Request access" instead of
   letting you add it directly, the repo owner (or an org admin) has to
   approve that request on their end — this is the #1 suspect if the
   Railway account logged in is *not* the same identity as `kaan7305`.
4. Back in Railway: Project → service → **Settings** → **Source**, click
   **Disconnect**, then **Connect Repo** again to force a fresh read of
   the (now-corrected) access list. Cached access lists can take a few
   minutes to refresh even after fixing the GitHub side.

### 3. Builder auto-detection (Nixpacks → Railpack)

Railway's default builder moved from Nixpacks to **Railpack** in 2025;
Nixpacks is now in maintenance mode. `railway.json` was pinning
`"builder": "NIXPACKS"`, which Railway still honors for existing configs,
but this repo also has a `package.json` two directories down
(`src/dispatch/web/desktop/`) and a large unrelated `site/` directory,
which is exactly the kind of repo shape that trips up zero-config
detection (wrong language guessed, wrong root, etc.) — and that class of
failure *can* surface as an immediate error before any build log is
emitted, depending on which detection stage fails.

**Fix (already applied in this repo):** `railway.json` now pins
`"builder": "DOCKERFILE"` pointing at the root `Dockerfile`, which
sidesteps Nixpacks/Railpack auto-detection entirely — Railway just builds
exactly what the Dockerfile says. This also makes `runtime.txt` and the
`Procfile` non-authoritative for Railway specifically (they're kept as
harmless fallback docs for other platforms); the Dockerfile is now the
single source of truth for the Python version (3.11) and start command.

### 4. Repo bloat (checked, ruled out)

Investigated because Railway's CLI (`railway up`) source uploads do have a
payload-size limit that fails fast on bloated repos. **This repo doesn't
have that problem**: `git ls-files | wc -l` → 184 files, tracked content
totals **6.9 MB**, `.git` is **28 MB**. `node_modules/` is correctly
untracked. The one large tracked directory
(`src/dispatch/web/desktop/dist/`, ~900 KB of built JS/CSS) is
**intentionally** committed — see the comment in `.gitignore` — because
the daemon serves that bundle directly and there's no frontend build step
at `pipx install` time; removing it from git would break the desktop UI
for every recipient daemon. `site/` (marketing site, images) and the PDF
at repo root are real content, not build artifacts, and aren't read by
the broker at all (confirmed: no reference to `site/` in
`broker/app.py`/`store.py`) — they're now excluded from the *Docker build
context* via `.dockerignore`, but were deliberately left tracked in git
since deleting them would delete the actual website. GitHub-source deploys
also don't share the CLI's upload-size failure mode — Railway clones from
GitHub directly — so repo size is very unlikely to be the actual cause of
the immediate error you're seeing, but the cleanup was worth doing anyway.

## Step-by-step: create the service

1. **Fix access first** — resolve #1 and #2 above before touching the
   Railway UI further; every subsequent step assumes the GitHub App can
   already see the repo and the account isn't Limited-Trial-restricted.
2. Railway dashboard → **New Project** → **Deploy from GitHub repo**.
3. Pick `kaan7305/dispatch`. If it's not in the list, that's #2 again —
   go fix the GitHub App grant.
4. Railway reads `railway.json` at the repo root and should show
   **Dockerfile** as the detected builder automatically. If it instead
   shows Nixpacks/Railpack, open the new service → **Settings** →
   **Build**, and manually set Builder = Dockerfile, Dockerfile path =
   `Dockerfile`.
5. **Settings → Networking** → **Generate Domain** so the service gets a
   public `*.up.railway.app` URL (this also populates
   `RAILWAY_PUBLIC_DOMAIN`, which `broker/app.py` reads to build absolute
   URLs when `DISPATCH_PUBLIC_URL` isn't set explicitly).
6. **Settings → Deploy**: confirm Healthcheck Path is `/health` and
   Restart Policy is "On Failure" (5 retries) — both already come from
   `railway.json`, just verify they landed.
7. Add environment variables (Project → service → **Variables**):

   | Variable | Required | Notes |
   |---|---|---|
   | `DATABASE_URL` | **Yes** | Postgres connection string, e.g. from Neon. `asyncpg` needs a standard `postgresql://user:pass@host/db?sslmode=require` URL — copy Neon's *pooled* connection string and keep the `sslmode=require` query param. Without this the broker raises `RuntimeError("DATABASE_URL is not set...")` on startup and the healthcheck will never pass. (Railway's own Postgres add-on also works and injects this variable for you — but see the free-plan note below: a Railway-hosted Postgres does *not* scale to zero the way Neon does.) |
   | `DISPATCH_JWT_SECRET` | **Yes** | 32+ random chars (`openssl rand -hex 32`). The broker mints the daemon/session tokens with it (`shared/identity.py`). **Easy to miss:** unlike `DATABASE_URL` this does *not* crash startup — `lifespan` only logs a warning, so the deploy goes green and `/health` returns `ok`, then every sign-in and device-auth approval fails with a 500 at runtime. Rotating it invalidates all issued tokens. |
   | `DISPATCH_PUBLIC_URL` | No | Overrides the auto-derived URL; only set if you're fronting Railway with a custom domain/proxy. |
   | `RAILWAY_PUBLIC_DOMAIN` | No | Set automatically by Railway once you generate a domain (step 5) — don't set it by hand. |
   | `CLERK_PUBLISHABLE_KEY`, `CLERK_FRONTEND_API`, `CLERK_JWT_TEMPLATE` | No | Only needed if the broker's web UI does Clerk auth; defaults to empty/`"dispatch"` and the `/config.js` route just ships blanks if unset. |
   | `RESEND_API_KEY`, `RESEND_FROM` | No | Email notifications; skipped silently if `RESEND_API_KEY` is absent. |
   | `TWILIO_ACCOUNT_SID`, `TWILIO_FROM_NUMBER`, `TWILIO_CHANNEL`, `TWILIO_CONTENT_SID`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_AUTH_TOKEN` | No | SMS/WhatsApp notifications; skipped silently if unset. |
   | `DISPATCH_DAEMON_INSTALL` | No | What `/install.sh` tells recipients to `pipx install`; defaults to a placeholder `git+https://github.com/your-org/dispatch.git` — set this to the real repo once it has a public URL, or recipients get pointed at a dead install target. |

8. Trigger a deploy (push to the connected branch, or **Deploy** in the
   UI). Watch **Deployments → \<latest\> → Build Logs** — with the
   Dockerfile builder you should now see real `docker build` output
   (`pip install -r requirements.txt`, etc.) instead of an instant,
   log-free failure. If it's *still* instant and log-free after all of
   the above, that's conclusive confirmation it's #1 (account-level) —
   contact `billing@railway.app`.

## Free-plan caveat: this service is not a good fit for the $1/mo Free tier

The broker holds **persistent WebSocket connections** from every connected
daemon (`/agent/connect`, plus sender watch sockets) — it's designed to be
always-on, not scale-to-zero. Per Railway's current pricing:

- The one-time **Trial** gives $5 in credit, good for ~30 days, then
  containers stop.
- After that, the **Free plan** gives only **$1/month** of credit. A
  single minimal always-on service (0.5 GB RAM / 0.5 vCPU) already costs
  roughly $0.80–$1.00/month baseline just sitting idle — before counting
  the extra compute from held-open WebSocket connections. You will almost
  certainly exhaust the $1 credit within days and the service will stop
  **without warning or automatic billing** (volumes/data are held ~30
  days before deletion).
- **Practically:** budget for the **Hobby plan** ($5/mo + usage) if this
  needs to stay up continuously, which also happens to be the same
  upgrade that clears the Limited Trial restriction in cause #1. Staying
  on Free is fine only for short-lived manual testing, not for a
  broker other machines depend on being reachable.

Postgres itself is not this concern (`DATABASE_URL` points at Neon, not a
Railway-hosted Postgres) — Neon's serverless compute scales to zero on its
own (the pool is configured with `min_size=0` for exactly that reason, see
`broker/store.py`), so it's only the Railway-hosted broker process itself
that needs the always-on budget accounted for.
