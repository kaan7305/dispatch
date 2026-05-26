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

-- Magic-link auth removed; sign-in is now Clerk (Google OAuth). The
-- broker still mints daemon JWTs via DISPATCH_JWT_SECRET. Old deployments
-- can drop the legacy table manually:  DROP TABLE IF EXISTS magic_links;

-- ============================================================================
-- Trust & device network (added additively; references users(user_id) TEXT).
-- ============================================================================

-- A user may run the daemon on several machines; each is a device with its
-- own Ed25519 signing keypair. Only the public key is ever stored here.
CREATE TABLE IF NOT EXISTS devices (
    device_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    public_key  BYTEA NOT NULL,                  -- Ed25519 public key, set once at enrollment
    status      TEXT NOT NULL DEFAULT 'active',  -- active | revoked
    last_seen   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

-- Directed trust edges: from_user may dispatch to to_user, within scopes.
CREATE TABLE IF NOT EXISTS trust_links (
    trust_link_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_user     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    to_user       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | revoked
    scopes        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_user, to_user)
);

CREATE INDEX IF NOT EXISTS idx_trust_to   ON trust_links(to_user)   WHERE status = 'accepted';
CREATE INDEX IF NOT EXISTS idx_trust_from ON trust_links(from_user) WHERE status = 'accepted';

-- How a trust edge gets created: an emailed single-use invite token.
CREATE TABLE IF NOT EXISTS invitations (
    invitation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_user     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    to_email      TEXT NOT NULL,
    token         TEXT UNIQUE NOT NULL,            -- high-entropy, single-use
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | declined | expired
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_invitations_token ON invitations(token);

-- Trust-layer columns on dispatches. Nullable so pre-trust-layer rows and the
-- schema's own idempotent re-runs stay valid.
ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS trust_link_id UUID REFERENCES trust_links(trust_link_id);
ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS sender_device UUID REFERENCES devices(device_id);
ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS target_device UUID REFERENCES devices(device_id);
ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS nonce         TEXT;
ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS signature     BYTEA;

-- Replay guard: a (sender_device, nonce) pair may be used at most once.
-- Partial so the legacy rows (both columns NULL) are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_nonce
    ON dispatches(sender_device, nonce)
    WHERE sender_device IS NOT NULL AND nonce IS NOT NULL;
