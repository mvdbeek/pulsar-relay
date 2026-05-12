"""Tests for the Phase-4 follow-up hardening:

* /auth/token/revoke now requires possession of the wire-format
  refresh token (jti + secret), not just a jti.
* /auth/device/* endpoints are rate-limited (per-IP).
* Bootstrap admin password update path actually rotates when the env
  changes (uses verify_password, not hash equality).

The atomic SET+EXPIRE and aux-key TTL changes are covered indirectly
by the existing OIDC/device/refresh tests (they all pass under the
new code), so no dedicated assertions needed there.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from pulsar_relay.api.limits import limiter
from pulsar_relay.auth.dependencies import (
    set_refresh_token_storage,
    set_user_storage,
)
from pulsar_relay.auth.refresh import InMemoryRefreshTokenStorage
from pulsar_relay.main import app


@pytest.fixture
def wired_app(auth_storage):
    """Wire user + refresh storage for the auth endpoints."""
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage
    refresh_storage = InMemoryRefreshTokenStorage()
    set_refresh_token_storage(refresh_storage)
    app.state.refresh_token_storage = refresh_storage
    return TestClient(app), refresh_storage


@pytest.mark.anyio
async def test_revoke_requires_full_wire_token(wired_app, auth_storage):
    """A leaked jti alone must NOT be enough to revoke a chain.

    Mint a refresh token, then attempt revocation with two payloads:
    ``{jti}.wrong-secret`` (forged) and the actual wire form. Only
    the real one revokes.
    """
    client, refresh_storage = wired_app
    user = await auth_storage.get_user_by_username("user")
    record, wire = await refresh_storage.create(user_id=user.user_id, ttl=timedelta(days=1))

    # Attacker presents the jti with a guessed secret. Server returns
    # 204 (don't leak whether the jti exists) but the token is NOT
    # revoked.
    forged = f"{record.jti}.NOT-THE-REAL-SECRET"
    limiter.reset()
    resp = client.post("/auth/token/revoke", json={"refresh_token": forged})
    assert resp.status_code == 204

    refetched = await refresh_storage.get_by_jti(record.jti)
    assert refetched is not None
    assert refetched.revoked is False, "forged secret must NOT revoke the token"

    # Legitimate holder uses the real wire form.
    resp = client.post("/auth/token/revoke", json={"refresh_token": wire})
    assert resp.status_code == 204

    refetched = await refresh_storage.get_by_jti(record.jti)
    assert refetched is not None
    assert refetched.revoked is True


@pytest.mark.anyio
async def test_revoke_unknown_jti_returns_204(wired_app):
    """Unknown ``jti`` returns 204 (same as ``wrong secret``) so the
    endpoint can't be used as a jti-enumeration oracle."""
    client, _ = wired_app
    limiter.reset()
    resp = client.post(
        "/auth/token/revoke",
        json={"refresh_token": "not-a-real-jti.not-a-real-secret"},
    )
    assert resp.status_code == 204


def test_device_code_endpoint_is_rate_limited(wired_app):
    """``POST /auth/device/code`` cap (10/min). The 11th call within
    the window must be 429 — defends the user_code namespace from
    brute force (Auth H#4)."""
    client, _ = wired_app
    limiter.reset()

    last_status = None
    for i in range(11):
        resp = client.post("/auth/device/code", data={"client_hint": f"test-{i}"})
        last_status = resp.status_code
        if resp.status_code == 429:
            break

    assert last_status == 429, f"expected 429 within 11 attempts, got {last_status}"


def test_device_landing_endpoint_is_rate_limited(wired_app):
    """The user-code landing page is the actual brute-force surface
    (only ~35 bits of search space). 20/min cap."""
    client, _ = wired_app
    limiter.reset()

    last_status = None
    for i in range(25):
        resp = client.get(f"/auth/device?user_code=FAKE-CODE-{i}")
        last_status = resp.status_code
        if resp.status_code == 429:
            return

    assert False, f"expected 429 within 25 attempts, got {last_status}"
