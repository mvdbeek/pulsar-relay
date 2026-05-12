"""End-to-end tests for /auth/login, /auth/token/refresh, /auth/sessions."""

import pytest

from pulsar_relay.auth.dependencies import get_device_code_storage


@pytest.mark.anyio
async def test_login_returns_refresh_token(test_client_with_auth):
    resp = test_client_with_auth.post(
        "/auth/login",
        data={"username": "admin", "password": "admin1234"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


@pytest.mark.anyio
async def test_refresh_rotates_token(test_client_with_auth):
    login = test_client_with_auth.post("/auth/login", data={"username": "admin", "password": "admin1234"}).json()
    refresh = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": login["refresh_token"]})
    assert refresh.status_code == 200
    rotated = refresh.json()
    assert rotated["access_token"]
    assert rotated["refresh_token"] != login["refresh_token"]  # rotated


@pytest.mark.anyio
async def test_replay_of_rotated_token_returns_401(test_client_with_auth):
    login = test_client_with_auth.post("/auth/login", data={"username": "admin", "password": "admin1234"}).json()
    test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": login["refresh_token"]})
    # Re-presenting the original (rotated) token must fail.
    replay = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": login["refresh_token"]})
    assert replay.status_code == 401


@pytest.mark.anyio
async def test_sessions_list_and_revoke(test_client_with_auth):
    login = test_client_with_auth.post("/auth/login", data={"username": "admin", "password": "admin1234"}).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    sessions = test_client_with_auth.get("/auth/sessions", headers=headers).json()
    assert len(sessions) >= 1
    target_jti = sessions[0]["jti"]

    delete = test_client_with_auth.delete(f"/auth/sessions/{target_jti}", headers=headers)
    assert delete.status_code == 204

    after = test_client_with_auth.get("/auth/sessions", headers=headers).json()
    assert all(s["jti"] != target_jti for s in after)


@pytest.mark.anyio
async def test_revoke_endpoint_kills_chain(test_client_with_auth):
    login = test_client_with_auth.post("/auth/login", data={"username": "admin", "password": "admin1234"}).json()
    rotated = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": login["refresh_token"]}).json()

    # Revoke the *new* token's chain. Old (already-rotated) is also dead.
    rev = test_client_with_auth.post(
        "/auth/token/revoke",
        json={"refresh_token": rotated["refresh_token"], "revoke_chain": True},
    )
    assert rev.status_code == 204
    next_attempt = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert next_attempt.status_code == 401


@pytest.mark.anyio
async def test_oidc_disabled_lists_no_providers(test_client_with_auth):
    resp = test_client_with_auth.get("/auth/oidc/providers")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_device_code_endpoint_requires_oidc(test_client_with_auth):
    """/auth/device/code refuses to issue codes without configured providers."""
    resp = test_client_with_auth.post("/auth/device/code")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "device_authorization_unavailable"


@pytest.mark.anyio
async def test_device_token_with_pair_issues_two_refresh_tokens(test_client_with_auth, admin_user):
    """Galaxy BYOC bootstrap path: when ``pair=true`` was set at
    /auth/device/code, the /auth/device/token response carries a
    ``refresh_token_secondary`` alongside ``refresh_token`` — two
    independent tokens, no shared rotation chain."""
    from datetime import timedelta

    storage = get_device_code_storage()
    record, device_code = await storage.create(
        verification_uri="https://relay.test/auth/device",
        verification_uri_complete_template="https://relay.test/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
        client_hint="byoc",
        pair=True,
    )
    # Simulate operator approval in lieu of the real OIDC redirect.
    await storage.approve(record.user_code, admin_user.user_id)

    resp = test_client_with_auth.post(
        "/auth/device/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_token_secondary"]
    assert body["refresh_token"] != body["refresh_token_secondary"]


@pytest.mark.anyio
async def test_device_token_without_pair_returns_single_token(test_client_with_auth, admin_user):
    """Backward-compat: callers that omit ``pair`` get the single-token
    response shape they got before this change."""
    from datetime import timedelta

    storage = get_device_code_storage()
    record, device_code = await storage.create(
        verification_uri="https://relay.test/auth/device",
        verification_uri_complete_template="https://relay.test/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
    )
    await storage.approve(record.user_code, admin_user.user_id)

    resp = test_client_with_auth.post(
        "/auth/device/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refresh_token"]
    assert "refresh_token_secondary" not in body


@pytest.mark.anyio
async def test_paired_refresh_tokens_rotate_independently(test_client_with_auth, admin_user):
    """The two refresh tokens issued together must rotate on separate
    chains: rotating one (and replaying it) must not revoke the other."""
    from datetime import timedelta

    storage = get_device_code_storage()
    record, device_code = await storage.create(
        verification_uri="https://relay.test/auth/device",
        verification_uri_complete_template="https://relay.test/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
        pair=True,
    )
    await storage.approve(record.user_code, admin_user.user_id)
    body = test_client_with_auth.post(
        "/auth/device/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    ).json()
    primary, secondary = body["refresh_token"], body["refresh_token_secondary"]

    # Rotate the primary; the rotation chain is local to the primary token,
    # not the secondary.
    rotated_primary = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": primary}).json()
    assert rotated_primary["refresh_token"] != primary

    # Replay of the original primary would normally revoke the user's whole
    # chain — but the secondary belongs to a different chain, so it must
    # still work after the replay attempt is rejected.
    replay = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": primary})
    assert replay.status_code == 401

    # Secondary still refreshes cleanly.
    rotated_secondary = test_client_with_auth.post("/auth/token/refresh", json={"refresh_token": secondary})
    assert rotated_secondary.status_code == 200, rotated_secondary.text
    assert rotated_secondary.json()["refresh_token"] != secondary
