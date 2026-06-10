"""Shared HTTP plumbing for daemon→broker proxy calls.

One pooled AsyncClient per local app instead of a client per request:
the TCP+TLS handshake to the broker (~50-150ms) dominates these small
proxied calls, so connection reuse is most of the perceived latency of
a UI tab click.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

# httpx's default keepalive_expiry is 5s — shorter than the gap between
# two UI interactions, so the pool would re-handshake on nearly every
# click. 60s survives normal tab-to-tab navigation; going much longer
# only raises the odds of picking a connection the broker's LB already
# closed (mitigated below, but still a wasted round trip).
_KEEPALIVE_EXPIRY_S = 60.0

# Methods safe to re-send when a pooled connection turns out to be dead
# mid-request. POST/PATCH are excluded: the broker may have processed
# the first attempt before the connection dropped, and e.g. a replayed
# compose would create a duplicate dispatch.
_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def make_broker_client() -> httpx.AsyncClient:
    """Pooled client for daemon→broker calls. Caller owns aclose()."""
    return httpx.AsyncClient(
        timeout=30.0,
        # retries=1 re-attempts connection *establishment* only
        # (ConnectError/ConnectTimeout) — safe for every method because
        # the request was never written.
        transport=httpx.AsyncHTTPTransport(
            retries=1,
            limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_S),
        ),
    )


async def broker_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json_body: Any = None,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> httpx.Response:
    """One proxied request on the shared pool.

    The broker's load balancer may close an idle keep-alive connection
    between requests; if we lose that race httpx raises
    RemoteProtocolError instead of reconnecting. httpcore evicts the
    dead connection from the pool on the failure, so a single retry for
    idempotent methods goes out on a fresh connection. Raises
    httpx.HTTPError on failure (callers map it to a 502).
    """
    try:
        return await client.request(
            method, url, json=json_body, params=params, headers=headers,
        )
    except httpx.RemoteProtocolError:
        if method.upper() not in _IDEMPOTENT:
            raise
        return await client.request(
            method, url, json=json_body, params=params, headers=headers,
        )
