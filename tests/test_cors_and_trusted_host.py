"""Tests for the CORS, TrustedHost, and body-size middlewares.

These middlewares are the load-bearing transport-layer fix for security
review Critical C4 ("no CORS / TrustedHost / Origin enforcement") and
API High #7 ("no payload caps"). The conftest configures
``PULSAR_ALLOWED_ORIGINS`` and ``PULSAR_TRUSTED_HOSTS`` so the test
suite can exercise the real allow-lists rather than starting middleware
fresh per test.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pulsar_relay.main import app


@pytest.fixture
async def anonymous_client():
    """Plain client — no auth header, base URL Host=test.

    ``test`` is in conftest's PULSAR_TRUSTED_HOSTS so the
    TrustedHostMiddleware accepts it; we override Host/Origin per test
    to exercise the rejection paths.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.anyio
async def test_cors_preflight_allowed_origin(anonymous_client):
    """Preflight from an allow-listed origin returns matching headers."""
    resp = await anonymous_client.options(
        "/health",
        headers={
            "Origin": "http://testserver",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Either 200 or 204 — preflight succeeds.
    assert resp.status_code in (200, 204), resp.text
    assert resp.headers.get("access-control-allow-origin") == "http://testserver"


@pytest.mark.anyio
async def test_cors_rejects_disallowed_origin(anonymous_client):
    """An origin not in the allow-list gets no CORS header.

    Browsers will reject the response client-side; the middleware
    intentionally does not 4xx so non-browser callers (which don't send
    ``Origin``) still work.
    """
    resp = await anonymous_client.get(
        "/health",
        headers={"Origin": "https://evil.example.com"},
    )
    # Endpoint still succeeds — CORS is browser-enforced.
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is None


@pytest.mark.anyio
async def test_trusted_host_rejects_unknown_host(anonymous_client):
    """A request with an unknown Host header is rejected by
    ``TrustedHostMiddleware`` with HTTP 400 — defends against
    Host-header spoofing when behind a reverse proxy."""
    resp = await anonymous_client.get(
        "/health",
        headers={"Host": "attacker.example.com"},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.anyio
async def test_body_size_middleware_rejects_oversize(anonymous_client):
    """Requests with Content-Length over the configured cap return 413
    before FastAPI buffers the body."""
    # Cap defaults to 1 MiB. Declare 2 MiB and watch the middleware
    # reject without ever reading the body.
    huge = b"a" * 100  # short body; we lie via the header
    resp = await anonymous_client.post(
        "/auth/login",
        headers={"Content-Length": str(2 * 1024 * 1024)},
        content=huge,
    )
    assert resp.status_code == 413, resp.text


@pytest.mark.anyio
async def test_body_size_middleware_passes_normal_request(anonymous_client):
    """Small requests pass through unchanged."""
    resp = await anonymous_client.get("/health")
    assert resp.status_code == 200
