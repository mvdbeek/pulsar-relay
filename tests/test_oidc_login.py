"""Tests for the OIDC RP client + auto-provisioning federation logic.

We mint our own RS256 ID tokens against a stubbed IdP (via ``respx`` for the
JWKS endpoint) so we exercise signature validation, claim checking, and the
federation handoff into the user storage.
"""

import time

import httpx
import pytest
import respx
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import RSAKey

from pulsar_relay.auth.federation import login_or_provision_oidc_user
from pulsar_relay.auth.oidc_client import OIDCClient, OIDCError
from pulsar_relay.auth.storage import InMemoryUserStorage
from pulsar_relay.config import OIDCConfig, OIDCProviderConfig

ISSUER = "https://idp.example"
CLIENT_ID = "pulsar-relay-test"


@pytest.fixture
def signing_key():
    """Stable RSA key with a known kid used to sign synthetic ID tokens."""
    return RSAKey.generate_key(2048, parameters={"kid": "test-key"})


@pytest.fixture
def jwks(signing_key):
    return {"keys": [signing_key.as_dict(private=False)]}


@pytest.fixture
def provider_config():
    return OIDCProviderConfig(
        display_name="Test IdP",
        client_id=CLIENT_ID,
        client_secret="shh",
        issuer=ISSUER,
        authorization_endpoint=f"{ISSUER}/auth",
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=f"{ISSUER}/jwks",
        userinfo_endpoint=f"{ISSUER}/userinfo",
    )


@pytest.fixture
def oidc_config():
    return OIDCConfig(
        enabled=True,
        base_url="https://relay.test",
        default_permissions=["read", "write"],
    )


def _mint(claims: dict, key) -> str:
    return joserfc_jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key)


def _good_claims() -> dict:
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-42",
        "email": "alice@example.com",
        "exp": now + 600,
        "iat": now,
        "nbf": now - 5,
        "nonce": "NONCE",
    }


@pytest.mark.anyio
async def test_validate_id_token_happy_path(signing_key, jwks, provider_config):
    async with httpx.AsyncClient() as http:
        client = OIDCClient("test", provider_config, http_client=http)
        with respx.mock(base_url=ISSUER) as router:
            router.get("/jwks").respond(json=jwks)
            claims = await client.validate_id_token(_mint(_good_claims(), signing_key), nonce="NONCE")
            assert claims["sub"] == "user-42"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tweak,error_substr",
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "different-client"}, "audience"),
        ({"exp": int(time.time()) - 600}, "expired"),
    ],
)
async def test_validate_id_token_rejects_bad_claims(signing_key, jwks, provider_config, tweak, error_substr):
    async with httpx.AsyncClient() as http:
        client = OIDCClient("test", provider_config, http_client=http)
        with respx.mock(base_url=ISSUER) as router:
            router.get("/jwks").respond(json=jwks)
            claims = {**_good_claims(), **tweak}
            with pytest.raises(OIDCError, match=error_substr):
                await client.validate_id_token(_mint(claims, signing_key), nonce="NONCE")


@pytest.mark.anyio
async def test_validate_id_token_rejects_nonce_mismatch(signing_key, jwks, provider_config):
    async with httpx.AsyncClient() as http:
        client = OIDCClient("test", provider_config, http_client=http)
        with respx.mock(base_url=ISSUER) as router:
            router.get("/jwks").respond(json=jwks)
            with pytest.raises(OIDCError, match="nonce"):
                await client.validate_id_token(_mint(_good_claims(), signing_key), nonce="WRONG")


@pytest.mark.anyio
async def test_validate_id_token_rejects_bad_signature(signing_key, jwks, provider_config):
    """A token signed by a *different* key should be rejected."""
    other_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    bad_token = joserfc_jwt.encode({"alg": "RS256", "kid": "test-key"}, _good_claims(), other_key)
    async with httpx.AsyncClient() as http:
        client = OIDCClient("test", provider_config, http_client=http)
        with respx.mock(base_url=ISSUER) as router:
            # Both initial fetch and forced refresh return our (stale wrt
            # the bad token) JWKS, so the retry branch fails too.
            router.get("/jwks").respond(json=jwks)
            with pytest.raises(OIDCError, match="signature"):
                await client.validate_id_token(bad_token, nonce="NONCE")


# --- federation / auto-provisioning ----------------------------------------


@pytest.mark.anyio
async def test_provision_new_user(provider_config, oidc_config):
    storage = InMemoryUserStorage()
    user = await login_or_provision_oidc_user(
        storage,
        provider_name="test",
        provider_config=provider_config,
        oidc_config=oidc_config,
        claims=_good_claims(),
    )
    assert user.username == "alice@example.com"
    assert user.email == "alice@example.com"
    assert user.permissions == ["read", "write"]
    assert user.hashed_password is None
    assert len(user.federated_identities) == 1
    assert user.federated_identities[0].sub == "user-42"


@pytest.mark.anyio
async def test_provision_idempotent(provider_config, oidc_config):
    storage = InMemoryUserStorage()
    a = await login_or_provision_oidc_user(
        storage,
        provider_name="test",
        provider_config=provider_config,
        oidc_config=oidc_config,
        claims=_good_claims(),
    )
    b = await login_or_provision_oidc_user(
        storage,
        provider_name="test",
        provider_config=provider_config,
        oidc_config=oidc_config,
        claims=_good_claims(),
    )
    assert a.user_id == b.user_id


@pytest.mark.anyio
async def test_provision_username_collision_suffixes(provider_config, oidc_config):
    storage = InMemoryUserStorage()
    # User A, sub=42
    await login_or_provision_oidc_user(
        storage,
        provider_name="test",
        provider_config=provider_config,
        oidc_config=oidc_config,
        claims=_good_claims(),
    )
    # User B, same email/username but different sub at same issuer.
    other = {**_good_claims(), "sub": "user-99"}
    user_b = await login_or_provision_oidc_user(
        storage,
        provider_name="test",
        provider_config=provider_config,
        oidc_config=oidc_config,
        claims=other,
    )
    assert user_b.username != "alice@example.com"
    assert "test" in user_b.username  # provider-suffixed


@pytest.mark.anyio
async def test_provision_requires_iss_and_sub(provider_config, oidc_config):
    storage = InMemoryUserStorage()
    with pytest.raises(ValueError, match="iss"):
        await login_or_provision_oidc_user(
            storage,
            provider_name="test",
            provider_config=provider_config,
            oidc_config=oidc_config,
            claims={"email": "bob@example.com"},
        )
