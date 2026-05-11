"""End-to-end OIDC tests against a real Keycloak."""

from __future__ import annotations

import threading
import time
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest

from .keycloak_login_helper import login_via_keycloak

pytestmark = pytest.mark.e2e


def test_oidc_browser_signin_provisions_user_and_returns_tokens(relay_against_keycloak):
    """A browser-style auth-code flow against a live Keycloak should
    auto-provision the user with read+write and hand back access+refresh."""
    relay = relay_against_keycloak["base_url"]
    setup = relay_against_keycloak["keycloak"]

    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        # 1. Operator hits /auth/oidc/keycloak/login → relay redirects to KC.
        start = client.get(f"{relay}/auth/oidc/keycloak/login")
        assert start.status_code == 302
        kc_auth_url = start.headers["location"]
        assert urlparse(kc_auth_url).netloc == urlparse(setup.base_url).netloc

        # 2. Drive Keycloak's HTML form, then follow the redirect back to the relay's callback.
        callback_resp = login_via_keycloak(
            authorization_url=kc_auth_url,
            username=setup.user_username,
            password=setup.user_password,
            follow_relay_callback=True,
        )

    assert callback_resp.status_code == 200, callback_resp.text
    body = callback_resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"

    # The relay should now have provisioned alice with default permissions.
    me = httpx.get(
        f"{relay}/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
        timeout=5.0,
    )
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["username"] == setup.user_username
    assert set(me_body["permissions"]) >= {"read", "write"}


def test_oidc_callback_is_idempotent(relay_against_keycloak):
    """Two browser sign-ins for the same Keycloak user resolve to the same relay user."""
    relay = relay_against_keycloak["base_url"]
    setup = relay_against_keycloak["keycloak"]

    def _signin() -> str:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            start = client.get(f"{relay}/auth/oidc/keycloak/login")
        cb = login_via_keycloak(
            authorization_url=start.headers["location"],
            username=setup.user_username,
            password=setup.user_password,
            follow_relay_callback=True,
        )
        body = cb.json()
        return cast(str, body["access_token"])

    me_a = httpx.get(
        f"{relay}/auth/me",
        headers={"Authorization": f"Bearer {_signin()}"},
        timeout=5.0,
    ).json()
    me_b = httpx.get(
        f"{relay}/auth/me",
        headers={"Authorization": f"Bearer {_signin()}"},
        timeout=5.0,
    ).json()
    assert me_a["user_id"] == me_b["user_id"]


def test_device_flow_end_to_end(relay_against_keycloak):
    """Full RFC 8628 dance: daemon polls /auth/device/token while a parallel
    'operator' completes Keycloak sign-in via the relay's bridge URL."""
    relay = relay_against_keycloak["base_url"]
    setup = relay_against_keycloak["keycloak"]

    with httpx.Client(timeout=10.0) as client:
        # 1. Daemon requests a device code.
        dev = client.post(
            f"{relay}/auth/device/code",
            data={"client_hint": "e2e-test"},
        )
        assert dev.status_code == 200, dev.text
        body = dev.json()
        device_code = body["device_code"]
        user_code = body["user_code"]
        interval = max(int(body["interval"]), 1)

    # 2. In a background thread, simulate an operator clicking the Keycloak
    #    button on the device approval page and signing in.
    operator_error: list[Exception] = []

    def operator():
        try:
            # The /auth/device GET page gives us provider buttons; we know the
            # provider name is "keycloak" so we kick off the OIDC start URL
            # carrying the device_user_code, exactly as that page would.
            with httpx.Client(timeout=10.0, follow_redirects=False) as op:
                start = op.get(
                    f"{relay}/auth/oidc/keycloak/login",
                    params={"device_user_code": user_code},
                )
                assert start.status_code == 302
                # The form-driver follows the redirect back into the relay's
                # callback, which approves the device session and renders the
                # "you may close this tab" HTML.
                final = login_via_keycloak(
                    authorization_url=start.headers["location"],
                    username=setup.user_username,
                    password=setup.user_password,
                    follow_relay_callback=True,
                )
                assert final.status_code == 200
                assert "complete" in final.text.lower()
        except Exception as exc:
            operator_error.append(exc)

    op_thread = threading.Thread(target=operator)
    op_thread.start()

    # 3. Poll the token endpoint until success or the operator fails.
    deadline = time.time() + 60
    final_tokens: dict | None = None
    with httpx.Client(timeout=10.0) as client:
        while time.time() < deadline:
            time.sleep(interval)
            poll = client.post(
                f"{relay}/auth/device/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
            )
            if poll.status_code == 200:
                final_tokens = poll.json()
                break
            err = poll.json().get("error", "")
            if err in ("authorization_pending", "slow_down"):
                if err == "slow_down":
                    interval += 5
                continue
            pytest.fail(f"Unexpected device-flow error: {poll.status_code} {poll.text}")
            break

    op_thread.join(timeout=10)
    if operator_error:
        raise operator_error[0]
    assert final_tokens is not None, "device-flow polling never completed"
    assert final_tokens["access_token"]
    assert final_tokens["refresh_token"]


def test_refresh_token_rotation_against_real_relay(relay_against_keycloak):
    """After the device flow, the refresh token must rotate cleanly."""
    relay = relay_against_keycloak["base_url"]

    # Faster path: use /auth/login + the bootstrap admin to skip the OIDC dance.
    with httpx.Client(timeout=10.0) as client:
        login = client.post(
            f"{relay}/auth/login",
            data={"username": "admin", "password": "adminpw1234"},
        )
        assert login.status_code == 200, login.text
        first = login.json()
        assert first["refresh_token"]

        rotated = client.post(
            f"{relay}/auth/token/refresh",
            json={"refresh_token": first["refresh_token"]},
        )
        assert rotated.status_code == 200
        second = rotated.json()
        assert second["refresh_token"] != first["refresh_token"]

        # Replay must fail.
        replay = client.post(
            f"{relay}/auth/token/refresh",
            json={"refresh_token": first["refresh_token"]},
        )
        assert replay.status_code == 401
