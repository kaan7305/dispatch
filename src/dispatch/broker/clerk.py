"""Clerk session-token verification.

Browser sign-in is handled by Clerk (Google OAuth). Clerk hands the SPA
a short-lived session JWT signed by Clerk's keys. The SPA posts that JWT
to /auth/clerk; this module verifies it against Clerk's JWKS, validates
issuer/expiry, and pulls the user's verified email out.

The email is expected to be present as a custom claim — configure a Clerk
JWT template (e.g. named "dispatch") that includes:

    { "email": "{{user.primary_email_address}}" }

That avoids a Clerk Backend API roundtrip per sign-in.

Env:
    CLERK_FRONTEND_API   e.g. "your-app.clerk.accounts.dev" (no scheme)
    CLERK_JWT_AUDIENCE   optional; if set on the JWT template, must match
"""
from __future__ import annotations

import os
from typing import Optional

import jwt
from jwt import PyJWKClient


class ClerkAuthError(Exception):
    pass


_JWKS_CACHE: dict[str, PyJWKClient] = {}


def _frontend_api() -> str:
    raw = os.environ.get("CLERK_FRONTEND_API")
    if not raw:
        raise ClerkAuthError(
            "CLERK_FRONTEND_API is not set. Add it to .env "
            "(e.g. your-app.clerk.accounts.dev)."
        )
    return raw.strip().rstrip("/").removeprefix("https://").removeprefix("http://")


def _issuer() -> str:
    return f"https://{_frontend_api()}"


def _jwks_client() -> PyJWKClient:
    api = _frontend_api()
    client = _JWKS_CACHE.get(api)
    if client is None:
        client = PyJWKClient(f"{_issuer()}/.well-known/jwks.json", cache_keys=True)
        _JWKS_CACHE[api] = client
    return client


def _audience() -> Optional[str]:
    aud = os.environ.get("CLERK_JWT_AUDIENCE")
    return aud.strip() if aud else None


def verify_clerk_token(token: str) -> dict:
    """Validate a Clerk session JWT. Returns the decoded claims dict.

    Raises ClerkAuthError on any failure (bad signature, wrong issuer,
    expired, JWKS unreachable, etc.)."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
    except Exception as e:
        raise ClerkAuthError(f"Could not resolve signing key: {e}") from e

    decode_kwargs: dict = {
        "key": signing_key.key,
        "algorithms": ["RS256"],
        "issuer": _issuer(),
        "options": {"require": ["exp", "iat"]},
    }
    aud = _audience()
    if aud:
        decode_kwargs["audience"] = aud

    try:
        return jwt.decode(token, **decode_kwargs)
    except jwt.InvalidTokenError as e:
        raise ClerkAuthError(f"Invalid Clerk token: {e}") from e


def extract_email(claims: dict) -> str:
    """Pull the verified primary email out of the Clerk claims. Requires the
    JWT template to expose it as `email` (or `primary_email_address`)."""
    raw = claims.get("email") or claims.get("primary_email_address")
    if not isinstance(raw, str) or "@" not in raw:
        raise ClerkAuthError(
            "Clerk token has no `email` claim — add it to the JWT template."
        )
    return raw.strip().lower()
