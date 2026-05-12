"""Tests for the refresh-token rotation race fix (Storage H#7).

The race window was: two concurrent /auth/token/refresh calls both
``verify_and_consume`` the same wire token, see ``revoked=0``, then both
proceed to mark it rotated and issue child tokens. The fix introduces
:meth:`RefreshTokenStorage.try_mark_rotated`, an atomic CAS, so exactly
one caller observes the transition.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from pulsar_relay.auth.refresh import InMemoryRefreshTokenStorage


@pytest.mark.anyio
async def test_concurrent_rotations_only_one_wins() -> None:
    """Two concurrent ``try_mark_rotated`` calls on the same jti must
    produce exactly one True. The losing caller learns the race was
    lost via False and can trigger chain revocation."""
    storage = InMemoryRefreshTokenStorage()
    token, _ = await storage.create(user_id="u-1", ttl=timedelta(days=1))

    # Fire the two CAS attempts back-to-back. The InMemoryRefreshTokenStorage
    # guards try_mark_rotated with an asyncio.Lock, so serialization is
    # explicit; the test asserts the *visible* CAS semantics rather than
    # the locking implementation.
    results = await asyncio.gather(
        storage.try_mark_rotated(token.jti),
        storage.try_mark_rotated(token.jti),
    )
    assert sorted(results) == [False, True]


@pytest.mark.anyio
async def test_try_mark_rotated_sets_revoked_reason() -> None:
    """The winning caller's transition must end with the rotated reason
    so a later replay attempt hits the chain-revocation branch in
    :func:`verify_and_consume`."""
    storage = InMemoryRefreshTokenStorage()
    token, _ = await storage.create(user_id="u-2", ttl=timedelta(days=1))

    assert await storage.try_mark_rotated(token.jti) is True
    record = await storage.get_by_jti(token.jti)
    assert record is not None
    assert record.revoked is True
    assert record.revoked_reason == "rotated"


@pytest.mark.anyio
async def test_try_mark_rotated_returns_false_for_unknown_jti() -> None:
    storage = InMemoryRefreshTokenStorage()
    assert await storage.try_mark_rotated("nonexistent-jti") is False


@pytest.mark.anyio
async def test_try_mark_rotated_returns_false_for_revoked_token() -> None:
    """Defence in depth: a token previously revoked for any reason
    (logout, replay, expired) cannot be rotated."""
    storage = InMemoryRefreshTokenStorage()
    token, _ = await storage.create(user_id="u-3", ttl=timedelta(days=1))
    await storage.mark_revoked(token.jti, "logout")
    assert await storage.try_mark_rotated(token.jti) is False
