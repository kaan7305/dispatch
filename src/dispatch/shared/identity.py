"""JWT-based identity. The broker is the identity provider.

For the demo, /auth/login takes any username and issues a JWT. To swap
to a real OAuth provider (GitHub, Google) later, replace issue_token
and verify_token; nothing else in the codebase touches JWTs directly.

The signing secret comes from DISPATCH_JWT_SECRET. Only the broker
process needs the secret. Daemons just carry the token they were given.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt

JWT_ALGORITHM = "HS256"
DEFAULT_TTL = timedelta(days=7)


class IdentityError(Exception):
    pass


def _secret() -> str:
    secret = os.environ.get("DISPATCH_JWT_SECRET")
    if not secret:
        raise IdentityError(
            "DISPATCH_JWT_SECRET is not set. Add it to .env "
            "(any random string of 32+ characters)."
        )
    return secret


def issue_token(user_id: str, *, ttl: timedelta = DEFAULT_TTL) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> str:
    """Returns the user_id (sub claim) if the token is valid."""
    sub, _ = verify_token_with_iat(token)
    return sub


def verify_token_with_iat(token: str) -> tuple[str, int]:
    """Returns (user_id, issued_at_unix_seconds) if the token is valid.

    Callers that need to check the JWT against a server-side revocation
    timestamp use this; everyone else uses verify_token().
    """
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError as e:
        raise IdentityError(f"Invalid token: {e}") from e
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise IdentityError("Token missing 'sub' claim")
    iat = int(payload.get("iat") or 0)
    return user_id, iat
