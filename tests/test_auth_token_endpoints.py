"""End-to-end tests for /auth/login, /auth/token/refresh, /auth/sessions."""

import pytest


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
    login = test_client_with_auth.post(
        "/auth/login", data={"username": "admin", "password": "admin1234"}
    ).json()
    refresh = test_client_with_auth.post(
        "/auth/token/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert refresh.status_code == 200
    rotated = refresh.json()
    assert rotated["access_token"]
    assert rotated["refresh_token"] != login["refresh_token"]  # rotated


@pytest.mark.anyio
async def test_replay_of_rotated_token_returns_401(test_client_with_auth):
    login = test_client_with_auth.post(
        "/auth/login", data={"username": "admin", "password": "admin1234"}
    ).json()
    test_client_with_auth.post(
        "/auth/token/refresh", json={"refresh_token": login["refresh_token"]}
    )
    # Re-presenting the original (rotated) token must fail.
    replay = test_client_with_auth.post(
        "/auth/token/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert replay.status_code == 401


@pytest.mark.anyio
async def test_sessions_list_and_revoke(test_client_with_auth):
    login = test_client_with_auth.post(
        "/auth/login", data={"username": "admin", "password": "admin1234"}
    ).json()
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
    login = test_client_with_auth.post(
        "/auth/login", data={"username": "admin", "password": "admin1234"}
    ).json()
    rotated = test_client_with_auth.post(
        "/auth/token/refresh", json={"refresh_token": login["refresh_token"]}
    ).json()

    # Revoke the *new* token's chain. Old (already-rotated) is also dead.
    rev = test_client_with_auth.post(
        "/auth/token/revoke",
        json={"refresh_token": rotated["refresh_token"], "revoke_chain": True},
    )
    assert rev.status_code == 204
    next_attempt = test_client_with_auth.post(
        "/auth/token/refresh", json={"refresh_token": rotated["refresh_token"]}
    )
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
