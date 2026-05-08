"""Provision a Keycloak realm + client + user via the admin REST API.

Used by the e2e fixtures so the test suite owns its own realm and we don't
have to ship a fragile realm-import JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class KeycloakSetup:
    base_url: str           # e.g. "http://localhost:8089"
    admin_user: str = "admin"
    admin_password: str = "adminpassword"
    realm: str = "pulsar-test"
    client_id: str = "pulsar-relay"
    client_secret: str = "pulsar-test-secret"
    user_username: str = "alice"
    user_password: str = "alicepass"
    user_email: str = "alice@example.com"

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{self.realm}"

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"


def _admin_token(client: httpx.Client, setup: KeycloakSetup) -> str:
    resp = client.post(
        f"{setup.base_url}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "username": setup.admin_user,
            "password": setup.admin_password,
            "grant_type": "password",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _put_or_post(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    token: str,
    body: dict,
    expected_201_or_409: bool = True,
) -> httpx.Response:
    resp = client.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    if expected_201_or_409 and resp.status_code in (201, 204, 409):
        return resp
    resp.raise_for_status()
    return resp


def provision(redirect_uris: list[str], setup: Optional[KeycloakSetup] = None) -> KeycloakSetup:
    """Idempotently create the test realm/client/user.

    ``redirect_uris`` must include the relay's callback URL (so Keycloak will
    redirect back after a successful sign-in).
    """
    if setup is None:
        setup = KeycloakSetup(base_url="http://localhost:8089")

    with httpx.Client(timeout=30.0) as client:
        token = _admin_token(client, setup)

        # 1. Realm.
        _put_or_post(
            client,
            method="POST",
            url=f"{setup.base_url}/admin/realms",
            token=token,
            body={"realm": setup.realm, "enabled": True},
        )

        # 2. Client (confidential, with PKCE/device flow enabled).
        _put_or_post(
            client,
            method="POST",
            url=f"{setup.base_url}/admin/realms/{setup.realm}/clients",
            token=token,
            body={
                "clientId": setup.client_id,
                "secret": setup.client_secret,
                "enabled": True,
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "directAccessGrantsEnabled": True,
                "standardFlowEnabled": True,
                "redirectUris": redirect_uris,
                "webOrigins": ["+"],
                "attributes": {
                    "oauth2.device.authorization.grant.enabled": "true",
                    "pkce.code.challenge.method": "S256",
                },
            },
        )

        # 3. User with a password.
        _put_or_post(
            client,
            method="POST",
            url=f"{setup.base_url}/admin/realms/{setup.realm}/users",
            token=token,
            body={
                "username": setup.user_username,
                "email": setup.user_email,
                "emailVerified": True,
                "enabled": True,
                "firstName": "Alice",
                "lastName": "Test",
                "credentials": [
                    {
                        "type": "password",
                        "value": setup.user_password,
                        "temporary": False,
                    }
                ],
            },
        )

    return setup


__all__ = ["KeycloakSetup", "provision"]
