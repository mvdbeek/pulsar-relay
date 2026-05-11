"""Tests for refresh-token issuance, rotation, and replay-detection."""

from datetime import timedelta

import pytest

from pulsar_relay.auth.refresh import (
    InMemoryRefreshTokenStorage,
    RefreshTokenError,
    split_wire_token,
    verify_and_consume,
)


@pytest.mark.anyio
async def test_create_returns_wire_token_with_jti_prefix():
    storage = InMemoryRefreshTokenStorage()
    record, wire = await storage.create(user_id="u1", ttl=timedelta(days=90))
    jti, secret = split_wire_token(wire)
    assert jti == record.jti
    assert secret  # non-empty


@pytest.mark.anyio
async def test_verify_round_trip():
    storage = InMemoryRefreshTokenStorage()
    record, wire = await storage.create(user_id="u1", ttl=timedelta(days=90))
    verified = await verify_and_consume(storage, wire)
    assert verified.jti == record.jti


@pytest.mark.anyio
async def test_secret_mismatch_rejected():
    storage = InMemoryRefreshTokenStorage()
    _, wire = await storage.create(user_id="u1", ttl=timedelta(days=90))
    jti, _ = split_wire_token(wire)
    bogus = f"{jti}.not-the-real-secret"
    with pytest.raises(RefreshTokenError, match="secret mismatch"):
        await verify_and_consume(storage, bogus)


@pytest.mark.anyio
async def test_malformed_token_rejected():
    storage = InMemoryRefreshTokenStorage()
    with pytest.raises(RefreshTokenError, match="malformed"):
        await verify_and_consume(storage, "no-dot-here")


@pytest.mark.anyio
async def test_expired_token_rejected_and_marked():
    storage = InMemoryRefreshTokenStorage()
    record, wire = await storage.create(user_id="u1", ttl=timedelta(seconds=-1))
    with pytest.raises(RefreshTokenError, match="expired"):
        await verify_and_consume(storage, wire)
    refreshed = await storage.get_by_jti(record.jti)
    assert refreshed.revoked is True
    assert refreshed.revoked_reason == "expired"


@pytest.mark.anyio
async def test_replay_of_rotated_token_revokes_chain():
    """The headline security property: presenting a rotated token kills the family."""
    storage = InMemoryRefreshTokenStorage()
    rec1, wire1 = await storage.create(user_id="u1", ttl=timedelta(days=90))

    # Simulate the server rotating the token (mark old as rotated, issue new).
    await storage.mark_revoked(rec1.jti, "rotated")
    rec2, wire2 = await storage.create(user_id="u1", ttl=timedelta(days=90), parent_jti=rec1.jti)

    # Until now, the new token is valid.
    valid = await verify_and_consume(storage, wire2)
    assert valid.jti == rec2.jti

    # An attacker presents the old (rotated) token. This must:
    # - reject the request, AND
    # - revoke the ENTIRE chain (including the currently-live successor).
    with pytest.raises(RefreshTokenError):
        await verify_and_consume(storage, wire1)
    after = await storage.get_by_jti(rec2.jti)
    assert after.revoked is True
    assert after.revoked_reason == "replay"


@pytest.mark.anyio
async def test_revoked_logout_token_rejected():
    storage = InMemoryRefreshTokenStorage()
    rec, wire = await storage.create(user_id="u1", ttl=timedelta(days=90))
    await storage.mark_revoked(rec.jti, "logout")
    with pytest.raises(RefreshTokenError, match="revoked"):
        await verify_and_consume(storage, wire)


@pytest.mark.anyio
async def test_list_for_user_filters_revoked_by_default():
    storage = InMemoryRefreshTokenStorage()
    a, _ = await storage.create(user_id="u1", ttl=timedelta(days=90))
    b, _ = await storage.create(user_id="u1", ttl=timedelta(days=90))
    await storage.mark_revoked(a.jti, "logout")

    live = await storage.list_for_user("u1")
    assert {t.jti for t in live} == {b.jti}

    all_tokens = await storage.list_for_user("u1", include_revoked=True)
    assert {t.jti for t in all_tokens} == {a.jti, b.jti}
