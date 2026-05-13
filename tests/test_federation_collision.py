"""Tests for the OIDC federation collision guard and email_verified gate.

Closes security review Auth H#3. Two attack patterns were previously
viable on a fresh deployment:

1. Username squatting: an IdP with ``preferred_username=admin`` claim
   would silently provision ``admin-keycloak`` (a NEW user with the
   configured default permissions) when a local ``admin`` already
   existed. Operators reading user lists could not easily tell the two
   apart.

2. Unverified email as identity: an IdP that accepts ``email=victim@
   example.com`` without proving control of the inbox would let the
   attacker claim ``victim@example.com`` as the relay username.
"""

from __future__ import annotations

import pytest

from pulsar_relay.auth.federation import FederationConflictError, login_or_provision_oidc_user
from pulsar_relay.auth.models import UserCreate
from pulsar_relay.auth.storage import InMemoryUserStorage
from pulsar_relay.config import OIDCConfig, OIDCProviderConfig


def _provider_config(**overrides) -> OIDCProviderConfig:
    base = dict(
        display_name="Test IdP",
        client_id="test-client",
        client_secret="test-secret",
        discovery_url="https://idp.test/.well-known/openid-configuration",
        claim_username="preferred_username",
        claim_email="email",
        claim_sub="sub",
    )
    base.update(overrides)
    return OIDCProviderConfig(**base)


def _oidc_config() -> OIDCConfig:
    return OIDCConfig(enabled=False, default_permissions=["read", "write"])


@pytest.mark.anyio
async def test_collision_with_password_account_raises():
    """An OIDC sign-in whose chosen username matches a local non-federated
    user must be rejected (was silently suffixed before)."""
    storage = InMemoryUserStorage()
    # Pre-existing password admin.
    await storage.create_user(
        UserCreate(username="admin", email="admin@example.com", password="adminpw1234", permissions=["admin"])
    )

    claims = {
        "iss": "https://idp.test",
        "sub": "subject-1",
        "preferred_username": "admin",
        "email": "ignored@idp.test",
        "email_verified": True,
    }
    with pytest.raises(FederationConflictError):
        await login_or_provision_oidc_user(
            storage,
            provider_name="keycloak",
            provider_config=_provider_config(),
            oidc_config=_oidc_config(),
            claims=claims,
        )


@pytest.mark.anyio
async def test_collision_with_existing_federated_user_falls_through():
    """A collision against an *already-federated* user falls through to a
    suffixed username — federated-to-federated collisions are expected
    when two IdPs both publish ``preferred_username=alice``."""
    storage = InMemoryUserStorage()
    # First federated sign-in claims "alice".
    await login_or_provision_oidc_user(
        storage,
        provider_name="github",
        provider_config=_provider_config(claim_username="preferred_username"),
        oidc_config=_oidc_config(),
        claims={
            "iss": "https://github.test",
            "sub": "gh-1",
            "preferred_username": "alice",
            "email_verified": True,
        },
    )

    # Second federated sign-in (different IdP) tries to claim "alice".
    user = await login_or_provision_oidc_user(
        storage,
        provider_name="keycloak",
        provider_config=_provider_config(),
        oidc_config=_oidc_config(),
        claims={
            "iss": "https://kc.test",
            "sub": "kc-1",
            "preferred_username": "alice",
            "email_verified": True,
        },
    )
    # Must NOT collide-with-local-password — distinct federated users are fine.
    assert user.username != "alice"
    assert "alice" in user.username


@pytest.mark.anyio
async def test_unverified_email_is_not_used_as_username():
    """An IdP that reports ``email_verified=False`` must not have its email
    claim used to derive the username."""
    storage = InMemoryUserStorage()
    user = await login_or_provision_oidc_user(
        storage,
        provider_name="keycloak",
        # Force "email" to be the chosen claim_username so we exercise the
        # email-verification gate.
        provider_config=_provider_config(claim_username="email"),
        oidc_config=_oidc_config(),
        claims={
            "iss": "https://idp.test",
            "sub": "subject-42",
            "email": "victim@example.com",
            "email_verified": False,
        },
    )
    # We must NOT have provisioned with the email as username.
    assert "@example.com" not in user.username
    # ``sub`` is the documented fallback when no acceptable source is available.
    assert "subject-42" in user.username or user.username.startswith("subject")


@pytest.mark.anyio
async def test_verified_email_can_be_used_as_username():
    """The sibling positive case: a verified email IS accepted."""
    storage = InMemoryUserStorage()
    user = await login_or_provision_oidc_user(
        storage,
        provider_name="keycloak",
        provider_config=_provider_config(claim_username="email"),
        oidc_config=_oidc_config(),
        claims={
            "iss": "https://idp.test",
            "sub": "subject-99",
            "email": "alice@example.com",
            "email_verified": True,
        },
    )
    assert "alice@example.com" in user.username or user.username == "alice@example.com"
