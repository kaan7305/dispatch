-- Dispatch broker schema (Postgres). Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id   UUID PRIMARY KEY,
    sender_id     TEXT NOT NULL,
    recipient_id  TEXT NOT NULL,
    task          TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatches_sender    ON dispatches(sender_id);
CREATE INDEX IF NOT EXISTS idx_dispatches_recipient ON dispatches(recipient_id);

CREATE TABLE IF NOT EXISTS dispatch_events (
    id           BIGSERIAL PRIMARY KEY,
    dispatch_id  UUID NOT NULL REFERENCES dispatches(dispatch_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    data         JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_dispatch ON dispatch_events(dispatch_id, seq);

CREATE TABLE IF NOT EXISTS pending_for_offline (
    user_id      TEXT NOT NULL,
    dispatch_id  UUID NOT NULL REFERENCES dispatches(dispatch_id) ON DELETE CASCADE,
    queued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, dispatch_id)
);

CREATE TABLE IF NOT EXISTS magic_links (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email);
