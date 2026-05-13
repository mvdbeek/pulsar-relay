"""Tests for the JWT access-token deny-list.

The deny-list is the load-bearing fix for security review Auth H#6: an
operator (or the holder of the token) can call ``/auth/logout`` to
revoke a leaked or stolen access token before its natural expiry. These
tests assert:

* JWTs issued by ``create_access_token`` carry a unique ``jti`` claim.
* ``InMemoryJWTDenylist`` correctly returns True for a freshly-added jti
  and False after the TTL expires.
* ``/auth/logout`` adds the bearer JWT's ``jti`` to the deny-list and
  subsequent requests bearing the same JWT return 401.
"""

from __future__ import annotations

import asyncio

import jwt
import pytest
from fastapi.testclient import TestClient

from pulsar_relay.auth.denylist import InMemoryJWTDenylist, seconds_until_exp
from pulsar_relay.auth.dependencies import set_jwt_denylist, set_user_storage
from pulsar_relay.auth.jwt import ALGORITHM, _get_secret_key, create_access_token
from pulsar_relay.main import app


@pytest.mark.anyio
async def test_in_memory_denylist_add_and_lookup() -> None:
    deny = InMemoryJWTDenylist()
    assert await deny.is_revoked("jti-1") is False
    await deny.add("jti-1", ttl_seconds=60)
    assert await deny.is_revoked("jti-1") is True


@pytest.mark.anyio
async def test_in_memory_denylist_expires_after_ttl() -> None:
    deny = InMemoryJWTDenylist()
    await deny.add("jti-2", ttl_seconds=0)
    # ttl_seconds=0 means the entry expires immediately on the next read.
    await asyncio.sleep(0.01)
    assert await deny.is_revoked("jti-2") is False


def test_create_access_token_has_unique_jti(test_user) -> None:
    """Every issued token carries a fresh UUID jti so it can be deny-listed
    independently of other concurrent sessions."""
    token1 = create_access_token(test_user)
    token2 = create_access_token(test_user)
    claims1 = jwt.decode(token1, _get_secret_key(), algorithms=[ALGORITHM])
    claims2 = jwt.decode(token2, _get_secret_key(), algorithms=[ALGORITHM])
    assert claims1["jti"]
    assert claims2["jti"]
    assert claims1["jti"] != claims2["jti"]


def test_seconds_until_exp_floors_at_zero() -> None:
    """Past timestamps must not produce a negative TTL."""
    assert seconds_until_exp(0) == 0


def test_logout_revokes_token(auth_storage) -> None:
    """End-to-end: log in, hit /auth/me OK, /auth/logout, /auth/me → 401."""
    deny = InMemoryJWTDenylist()
    set_user_storage(auth_storage)
    set_jwt_denylist(deny)

    # Resolve the existing 'user' test fixture and mint a JWT.
    user = asyncio.run(auth_storage.get_user_by_username("user"))
    token = create_access_token(user)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {token}"}

    # Pre-logout: /auth/me works.
    resp = client.get("/auth/me", headers=auth)
    assert resp.status_code == 200, resp.text

    # Logout deny-lists the current jti.
    logout_resp = client.post("/auth/logout", headers=auth)
    assert logout_resp.status_code == 204, logout_resp.text

    # Post-logout: same JWT now returns 401.
    resp_after = client.get("/auth/me", headers=auth)
    assert resp_after.status_code == 401
    assert "revoked" in resp_after.text.lower()


def test_logout_on_legacy_token_without_jti_is_noop(auth_storage) -> None:
    """Tokens issued before the jti claim landed (jti=None) cannot be
    deny-listed; logout returns 204 but does not raise."""
    deny = InMemoryJWTDenylist()
    set_user_storage(auth_storage)
    set_jwt_denylist(deny)

    # Mint a JWT manually without a jti claim — simulates the legacy
    # format. Pyjwt requires us to set sub/exp/iat by hand.
    import time

    legacy_payload = {
        "sub": asyncio.run(auth_storage.get_user_by_username("user")).user_id,
        "username": "user",
        "permissions": ["read", "write"],
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
    }
    legacy_token = jwt.encode(legacy_payload, _get_secret_key(), algorithm=ALGORITHM)
    client = TestClient(app)
    resp = client.post("/auth/logout", headers={"Authorization": f"Bearer {legacy_token}"})
    assert resp.status_code == 204
