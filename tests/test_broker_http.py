"""Pooled daemon→broker client: retry semantics for dead keep-alive
connections. RemoteProtocolError means the broker (or its LB) closed a
pooled connection under us — safe to replay idempotent methods exactly
once, never POST/PATCH (the first attempt may have been processed)."""
import asyncio

import httpx
import pytest

from dispatch.daemon.broker_http import broker_request, make_broker_client


def _flaky_client(fail_times: int, calls: list):
    """Client whose transport raises RemoteProtocolError for the first
    fail_times requests, then returns 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) <= fail_times:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.",
                request=request,
            )
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_get_retried_once_on_dead_connection():
    calls: list = []
    client = _flaky_client(1, calls)
    resp = asyncio.run(broker_request(client, "GET", "http://broker/trust"))
    assert resp.status_code == 200
    assert calls == ["GET", "GET"]


def test_get_not_retried_twice():
    calls: list = []
    client = _flaky_client(2, calls)
    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(broker_request(client, "GET", "http://broker/trust"))
    assert calls == ["GET", "GET"]


def test_post_never_replayed():
    calls: list = []
    client = _flaky_client(1, calls)
    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(broker_request(client, "POST", "http://broker/dispatch"))
    assert calls == ["POST"]


def test_make_broker_client_keepalive_outlives_ui_gaps():
    client = make_broker_client()
    try:
        # The whole point of the pool: connections must survive the gap
        # between two tab clicks, which httpx's 5s default does not.
        transport = client._transport
        assert transport._pool._keepalive_expiry >= 30
    finally:
        asyncio.run(client.aclose())
