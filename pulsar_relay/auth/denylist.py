"""Access-token deny-list.

Access tokens are short-lived (~10 minutes by default) so the cost of a
deny-list lookup on every request is bounded by the access-token TTL — a
client's stored deny-list entries never accumulate beyond that window.

Two backends:

* :class:`ValkeyJWTDenylist` — production. Keys are
  ``denylist:jti:{jti}`` with the relay's Valkey TTL. ``SET NX EX`` is
  used so a logout race doesn't overwrite an existing entry, and
  ``SISMEMBER`` is not — we use string keys with TTL so expiry is
  automatic.
* :class:`InMemoryJWTDenylist` — tests / memory backend.

The interface is intentionally narrow: ``add`` plus ``is_revoked``. The
storage layer never sees the JWT body — only its ``jti`` claim, which is
a UUID and therefore safe to log.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

logger = logging.getLogger(__name__)


class JWTDenylistStorage(Protocol):
    """Narrow contract for storing revoked access-token jtis."""

    async def add(self, jti: str, ttl_seconds: int) -> None:
        """Mark ``jti`` as revoked for ``ttl_seconds``."""

    async def is_revoked(self, jti: str) -> bool:
        """Return True if ``jti`` is currently in the deny-list."""


class InMemoryJWTDenylist:
    """Process-local deny-list backed by a dict.

    Used for in-memory test runs and as a fallback for single-worker
    deployments. Does NOT survive process restart (acceptable: access
    tokens themselves don't either, in practice — short TTL bounds the
    risk window).
    """

    def __init__(self) -> None:
        # jti -> expiry timestamp (seconds since epoch)
        self._revoked: dict[str, float] = {}

    async def add(self, jti: str, ttl_seconds: int) -> None:
        self._revoked[jti] = time.time() + max(0, ttl_seconds)

    async def is_revoked(self, jti: str) -> bool:
        expiry = self._revoked.get(jti)
        if expiry is None:
            return False
        if expiry < time.time():
            # Lazy cleanup — entries expire on next read.
            del self._revoked[jti]
            return False
        return True


class ValkeyJWTDenylist:
    """Valkey-backed deny-list.

    One key per revoked jti: ``denylist:jti:{jti}``. The key value is the
    revocation reason (currently always ``"logout"``); we don't read it
    anywhere — the existence of the key is the signal — but storing a
    string makes the key inspectable when operators tail Valkey for
    debugging.
    """

    _KEY_PREFIX = "denylist:jti:"

    def __init__(self, client) -> None:  # GlideClient — untyped to avoid import cycle
        self._client = client

    def _key(self, jti: str) -> str:
        return f"{self._KEY_PREFIX}{jti}"

    async def add(self, jti: str, ttl_seconds: int) -> None:
        # ``SET key value EX ttl`` — single atomic call that creates the
        # key and sets TTL. We do not use NX: a duplicate logout for the
        # same jti is harmless and updating the TTL keeps the entry alive
        # for the longest of the concurrent revocations.
        await self._client.set(self._key(jti), "logout", expiry={"type": "EX", "count": max(1, ttl_seconds)})

    async def is_revoked(self, jti: str) -> bool:
        value = await self._client.get(self._key(jti))
        return value is not None


def seconds_until_exp(exp_timestamp: int) -> int:
    """Seconds remaining until ``exp_timestamp`` (epoch). Floors at zero."""
    return max(0, int(exp_timestamp - time.time()))


__all__ = [
    "JWTDenylistStorage",
    "InMemoryJWTDenylist",
    "ValkeyJWTDenylist",
    "seconds_until_exp",
]
