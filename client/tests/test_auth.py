"""Tests for pulsar_relay_client.auth strategies.

Uses the ``responses`` lib to stub the relay endpoints.
"""

import json
import os

import pytest
import responses
from pulsar_relay_client.auth import (
    PasswordAuthenticator,
    RefreshTokenAuthenticator,
    RelayAuthError,
    RelayAuthManager,
)
from pulsar_relay_client.credentials import CredentialsFile, InMemoryCredentialsStore

RELAY_URL = "https://relay.test"


@responses.activate
def test_password_authenticator_captures_refresh_token(tmp_path):
    cred_path = str(tmp_path / "rel_cred.json")
    cred = CredentialsFile(cred_path)
    responses.add(
        responses.POST,
        f"{RELAY_URL}/auth/login",
        json={
            "access_token": "AT-1",
            "refresh_token": "JTI.SECRET",
            "token_type": "bearer",
            "expires_in": 3600,
        },
        status=200,
    )
    auth = PasswordAuthenticator(RELAY_URL, "user", "pw", credentials_file=cred)
    access, expires = auth.authenticate()
    assert access == "AT-1"
    assert expires == 3600
    saved = cred.load()
    assert saved["refresh_token"] == "JTI.SECRET"
    # File mode locked down.
    mode = os.stat(cred_path).st_mode & 0o777
    assert mode == 0o600


@responses.activate
def test_refresh_authenticator_rotates_credentials(tmp_path):
    cred_path = str(tmp_path / "rel_cred.json")
    cred = CredentialsFile(cred_path)
    cred.save({"relay_url": RELAY_URL, "refresh_token": "OLD-JTI.OLD-SEC"})

    responses.add(
        responses.POST,
        f"{RELAY_URL}/auth/token/refresh",
        json={
            "access_token": "AT-2",
            "refresh_token": "NEW-JTI.NEW-SEC",
            "token_type": "bearer",
            "expires_in": 3600,
        },
        status=200,
    )
    auth = RefreshTokenAuthenticator(RELAY_URL, cred)
    access, _ = auth.authenticate()
    assert access == "AT-2"
    saved = cred.load()
    # The credentials file was rewritten with the rotated token.
    assert saved["refresh_token"] == "NEW-JTI.NEW-SEC"


@responses.activate
def test_refresh_authenticator_raises_on_revoked(tmp_path):
    cred_path = str(tmp_path / "rel_cred.json")
    cred = CredentialsFile(cred_path)
    cred.save({"relay_url": RELAY_URL, "refresh_token": "REVOKED-JTI.REVOKED-SEC"})
    responses.add(
        responses.POST,
        f"{RELAY_URL}/auth/token/refresh",
        json={"detail": "invalid refresh token"},
        status=401,
    )
    auth = RefreshTokenAuthenticator(RELAY_URL, cred)
    with pytest.raises(RelayAuthError, match="rejected"):
        auth.authenticate()


def test_refresh_authenticator_missing_file(tmp_path):
    cred_path = str(tmp_path / "rel_cred.json")
    cred = CredentialsFile(cred_path)
    auth = RefreshTokenAuthenticator(RELAY_URL, cred)
    with pytest.raises(RelayAuthError, match="No refresh token"):
        auth.authenticate()


def test_relay_auth_manager_legacy_signature():
    """Existing callers that pass (relay_url, username, password) keep working."""
    m = RelayAuthManager(RELAY_URL, "u", "p")
    assert m.strategy_name == "password"


def test_relay_auth_manager_prefers_credentials_file(tmp_path):
    cred_path = str(tmp_path / "rel_cred.json")
    CredentialsFile(cred_path).save({"relay_url": RELAY_URL, "refresh_token": "JTI.SEC"})
    m = RelayAuthManager(RELAY_URL, credentials_file=cred_path)
    assert m.strategy_name == "refresh_token"


def test_relay_auth_manager_falls_back_to_password_when_no_cred_file(tmp_path):
    cred_path = str(tmp_path / "absent.json")
    m = RelayAuthManager(RELAY_URL, "u", "p", credentials_file=cred_path)
    assert m.strategy_name == "password"


def test_credentials_file_warns_on_loose_perms(tmp_path, caplog):
    cred_path = str(tmp_path / "rel_cred.json")
    with open(cred_path, "w") as f:
        json.dump({"refresh_token": "x.y"}, f)
    os.chmod(cred_path, 0o644)
    cred = CredentialsFile(cred_path)
    with caplog.at_level("WARNING", logger="pulsar_relay_client.credentials"):
        cred.load()
    assert any("mode" in rec.message for rec in caplog.records)


@responses.activate
def test_refresh_authenticator_with_in_memory_store_fires_callback():
    """The BYOC path: the multi-tenant runner hands a refresh token in
    memory and expects rotations delivered back via ``on_save`` so it can
    persist them to its own vault."""
    saved = []
    store = InMemoryCredentialsStore(
        relay_url=RELAY_URL,
        refresh_token="OLD-JTI.OLD-SEC",
        on_save=saved.append,
        label="<test>",
    )
    responses.add(
        responses.POST,
        f"{RELAY_URL}/auth/token/refresh",
        json={
            "access_token": "AT-2",
            "refresh_token": "ROTATED-JTI.ROTATED-SEC",
            "token_type": "bearer",
            "expires_in": 3600,
        },
        status=200,
    )

    auth = RefreshTokenAuthenticator(RELAY_URL, store)
    access, _ = auth.authenticate()

    assert access == "AT-2"
    assert len(saved) == 1
    assert saved[0]["refresh_token"] == "ROTATED-JTI.ROTATED-SEC"
    # Subsequent reads return the rotated token.
    assert store.load()["refresh_token"] == "ROTATED-JTI.ROTATED-SEC"


def test_relay_auth_manager_accepts_explicit_credentials_store():
    """BYOC path: caller hands a pre-built store; manager uses it."""
    store = InMemoryCredentialsStore(
        relay_url=RELAY_URL,
        refresh_token="X.Y",
    )
    m = RelayAuthManager(RELAY_URL, credentials_store=store)
    assert m.strategy_name == "refresh_token"
    assert m._authenticator._credentials_file is store  # type: ignore[attr-defined]
