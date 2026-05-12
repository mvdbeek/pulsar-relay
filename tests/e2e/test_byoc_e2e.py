"""End-to-end BYOC bootstrap against a real Keycloak + pulsar-relay.

Validates the pieces Galaxy depends on:

1. ``/auth/device/code`` with ``pair=true`` followed by Keycloak browser
   sign-in produces *two* independent refresh tokens.
2. Each refresh token rotates on its own chain — replay of one must
   not revoke the other (the property that makes pair-issuance safe
   for delegated use by Galaxy).
3. The user's access token (post-pair) can pin the three BYOC topics
   on the relay; a *different* user can't claim the same topic names
   (race-vs-attacker check).

Gated on ``-m e2e`` and skipped if Docker isn't reachable.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from .keycloak_login_helper import login_via_keycloak

pytestmark = pytest.mark.e2e


_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _drive_device_flow_with_pair(relay: str, setup) -> dict:
    """Drive a full RFC 8628 device flow with ``pair=true`` and return the
    final ``/auth/device/token`` body."""
    with httpx.Client(timeout=10.0) as client:
        dev = client.post(
            f"{relay}/auth/device/code",
            data={"client_hint": "byoc-e2e", "pair": "true"},
        )
        assert dev.status_code == 200, dev.text
        body = dev.json()
        device_code = body["device_code"]
        user_code = body["user_code"]
        interval = max(int(body["interval"]), 1)

    operator_error: list[Exception] = []

    def operator():
        try:
            with httpx.Client(timeout=10.0, follow_redirects=False) as op:
                start = op.get(
                    f"{relay}/auth/oidc/keycloak/login",
                    params={"device_user_code": user_code},
                )
                assert start.status_code == 302
                final = login_via_keycloak(
                    authorization_url=start.headers["location"],
                    username=setup.user_username,
                    password=setup.user_password,
                    follow_relay_callback=True,
                )
                assert final.status_code == 200
        except Exception as exc:
            operator_error.append(exc)

    op_thread = threading.Thread(target=operator)
    op_thread.start()
    final_body: dict | None = None
    try:
        deadline = time.time() + 60
        with httpx.Client(timeout=10.0) as client:
            while time.time() < deadline:
                time.sleep(interval)
                poll = client.post(
                    f"{relay}/auth/device/token",
                    data={"grant_type": _DEVICE_GRANT, "device_code": device_code},
                )
                if poll.status_code == 200:
                    final_body = poll.json()
                    break
                err = poll.json().get("error", "")
                if err in ("authorization_pending", "slow_down"):
                    if err == "slow_down":
                        interval += 5
                    continue
                pytest.fail(f"Unexpected device-flow error: {poll.status_code} {poll.text}")
    finally:
        op_thread.join(timeout=10)
        if operator_error:
            raise operator_error[0]
    assert final_body is not None, "device-flow polling never completed"
    return final_body


def test_byoc_device_flow_issues_independent_pair(relay_against_keycloak):
    """``pair=true`` returns two refresh tokens that survive each other's
    rotation + replay."""
    relay = relay_against_keycloak["base_url"]
    setup = relay_against_keycloak["keycloak"]

    body = _drive_device_flow_with_pair(relay, setup)
    primary = body["refresh_token"]
    secondary = body["refresh_token_secondary"]
    assert primary
    assert secondary
    assert primary != secondary

    with httpx.Client(timeout=10.0) as client:
        rotated_primary = client.post(f"{relay}/auth/token/refresh", json={"refresh_token": primary})
        assert rotated_primary.status_code == 200, rotated_primary.text

        # Replay of the original primary now revokes the primary's chain.
        replay = client.post(f"{relay}/auth/token/refresh", json={"refresh_token": primary})
        assert replay.status_code == 401

        # Secondary belongs to a separate chain — must still work.
        rotated_secondary = client.post(f"{relay}/auth/token/refresh", json={"refresh_token": secondary})
        assert rotated_secondary.status_code == 200, rotated_secondary.text
        assert rotated_secondary.json()["refresh_token"] != secondary


def test_byoc_topic_pinning_against_real_relay(relay_against_keycloak):
    """The BYOC user can create the three topics named for its ``sub``;
    a different user cannot claim them, but can read public state."""
    relay = relay_against_keycloak["base_url"]
    setup = relay_against_keycloak["keycloak"]

    body = _drive_device_flow_with_pair(relay, setup)
    access_token = body["access_token"]
    user_headers = {"Authorization": f"Bearer {access_token}"}

    # Look up the BYOC user_id so we can compare to topic.owner_id.
    me = httpx.get(f"{relay}/auth/me", headers=user_headers, timeout=5.0)
    assert me.status_code == 200
    byoc_user_id = me.json()["user_id"]
    sub = me.json()["username"]

    # 1. The BYOC user creates the three topics.
    for prefix in ("job_setup", "job_kill", "job_status_update"):
        topic_name = f"{prefix}_{sub}"
        resp = httpx.post(
            f"{relay}/api/v1/topics",
            headers={**user_headers, "Content-Type": "application/json"},
            json={"topic_name": topic_name},
            timeout=5.0,
        )
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["owner_id"] == byoc_user_id

    # 2. A different user (the bootstrap admin) tries to create the same
    #    topic names and is rejected — the BYOC user owns them.
    admin_login = httpx.post(
        f"{relay}/auth/login",
        data={"username": "admin", "password": "adminpw1234"},
        timeout=5.0,
    )
    assert admin_login.status_code == 200
    admin_headers = {
        "Authorization": f"Bearer {admin_login.json()['access_token']}",
        "Content-Type": "application/json",
    }
    for prefix in ("job_setup", "job_kill", "job_status_update"):
        topic_name = f"{prefix}_{sub}"
        resp = httpx.post(
            f"{relay}/api/v1/topics",
            headers=admin_headers,
            json={"topic_name": topic_name},
            timeout=5.0,
        )
        # The relay maps "already exists" to 400 — see api/topics.py.
        # Any 4xx is acceptable; the contract is "admin can't seize ownership".
        assert (
            400 <= resp.status_code < 500
        ), f"admin unexpectedly succeeded in claiming {topic_name}: {resp.status_code} {resp.text}"


def test_byoc_pin_topics_helper_against_real_relay(relay_against_keycloak):
    """Galaxy-side check: ``PulsarByocManager._pin_topics_for_manager``
    succeeds against a real relay when the topics don't exist yet, and is
    safely idempotent when re-run."""
    relay = relay_against_keycloak["base_url"]
    setup = relay_against_keycloak["keycloak"]

    # Import only on demand so the rest of the test file works without Galaxy
    # on the path — the e2e suite is sometimes run standalone.
    galaxy_manager = pytest.importorskip("galaxy.managers.pulsar_byoc")
    PulsarByocManager = galaxy_manager.PulsarByocManager  # noqa: N806

    body = _drive_device_flow_with_pair(relay, setup)
    access_token = body["access_token"]
    sub = httpx.get(
        f"{relay}/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5.0,
    ).json()["username"]

    # Bare manager — we only call ``_pin_topics_for_manager`` which doesn't
    # need DB/vault state.
    manager = object.__new__(PulsarByocManager)
    manager.app = None
    manager.session = None

    # First call creates topics.
    manager._pin_topics_for_manager(access_token, relay, sub)
    # Second call: topics now exist and are owned by us → must succeed.
    manager._pin_topics_for_manager(access_token, relay, sub)
