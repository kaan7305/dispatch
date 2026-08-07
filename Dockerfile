# Broker deploy image. Deliberately bypasses Railway's Nixpacks/Railpack
# auto-detection (the repo also contains a frontend package.json and a
# large marketing site/ dir that confuse zero-config builders) — this is
# the single, deterministic build path for `dispatch.broker.app`.
#
# NOTE: this image only needs the broker's Python deps and the already-built
# static assets under src/dispatch/web/ (web/app/, web/desktop/dist/) — the
# frontend source, node_modules, and pnpm-lock.yaml are NOT needed here
# because the desktop UI bundle is pre-built and committed to git
# (see .gitignore's `!/src/dispatch/web/desktop/dist/` exception).
FROM python:3.11-slim

WORKDIR /app

# System deps: keyring's SecretStorage backend and asyncpg both build fine
# without extra system packages on slim, but ship the C toolchain in case a
# future dependency needs to compile a wheel; keep the layer thin otherwise.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Only what the broker imports/serves at runtime: the package source
# (which carries broker/schema.sql and web/app + web/desktop/dist per
# pyproject's package-data) — not tests/, site/, docs/, the desktop
# frontend source, or anything else at the repo root.
COPY src/ ./src/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn --app-dir src dispatch.broker.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
