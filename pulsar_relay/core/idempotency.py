"""Idempotency-Key dedupe store.

The pulsar-relay-client v1.1 generates one ``Idempotency-Key`` UUID
per logical publish and re-uses it across retry attempts. The server
records ``(owner_id, idempotency_key) -> response_body`` for a short
window (default 10 minutes — comfortably longer than the client's
worst-case retry envelope of ~127s). A subsequent publish bearing the
same key returns the cached response instead of writing a duplicate
message (Client H#2).

Two backends — Valkey for production cross-worker dedupe, in-memory
for tests and single-worker dev. The interface is intentionally
narrow: ``try_claim`` (atomic check-and-set returning the cached body
on collision) plus ``record`` for the response payload.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, cast

logger = logging.getLogger(__name__)


class IdempotencyStorage(Protocol):
    """Storage contract for Idempotency-Key dedupe.

    Implementations must be safe under concurrent calls — the
    check-then-set in :meth:`try_claim` is the load-bearing primitive.
    """

    async def try_claim(self, owner_id: str, key: str, ttl_seconds: int) -> dict[str, Any] | None:
        """Atomically register ``(owner_id, key)`` as in-flight.

        Returns ``None`` if this call owned the transition (no prior
        record). Returns the previously-recorded response body if the
        ``(owner_id, key)`` pair has been seen before — the caller
        returns that body instead of writing a fresh message.
        """

    async def record(self, owner_id: str, key: str, response: dict[str, Any], ttl_seconds: int) -> None:
        """Store the response body so a later replay returns it verbatim."""


class InMemoryIdempotencyStorage:
    """Process-local dedupe store backed by a dict.

    Acceptable for tests and single-worker deployments. Multi-worker
    deployments need :class:`ValkeyIdempotencyStorage` so the cache is
    shared across workers — otherwise a retry can land on a different
    worker and still produce a duplicate.
    """

    _SENTINEL = "__in_flight__"

    def __init__(self) -> None:
        # (owner_id, key) -> (expiry_epoch, response_body | _SENTINEL)
        self._entries: dict[tuple[str, str], tuple[float, Any]] = {}

    def _fresh(self, entry: tuple[float, Any]) -> bool:
        return entry[0] > time.time()

    async def try_claim(self, owner_id: str, key: str, ttl_seconds: int) -> dict[str, Any] | None:
        composite = (owner_id, key)
        existing = self._entries.get(composite)
        if existing is not None and self._fresh(existing):
            body = existing[1]
            if body == self._SENTINEL:
                # Concurrent request still in flight; return an empty
                # cached body — the caller treats this same as "seen
                # before" and will not write again. Pragmatic
                # behaviour: at worst we drop one duplicate message.
                return {}
            return cast("dict[str, Any]", body)
        self._entries[composite] = (time.time() + max(1, ttl_seconds), self._SENTINEL)
        return None

    async def record(self, owner_id: str, key: str, response: dict[str, Any], ttl_seconds: int) -> None:
        self._entries[(owner_id, key)] = (time.time() + max(1, ttl_seconds), response)


class ValkeyIdempotencyStorage:
    """Valkey-backed dedupe shared across uvicorn workers.

    One key per ``(owner_id, idempotency_key)``: ``idem:{owner_id}/{key}``.
    Value is JSON; sentinel ``"__in_flight__"`` means the request is
    being processed. The atomic claim uses ``SET key value NX EX ttl``.
    """

    _SENTINEL = "__in_flight__"

    def __init__(self, client) -> None:  # GlideClient (untyped to avoid circular import)
        self._client = client

    @staticmethod
    def _key(owner_id: str, key: str) -> str:
        return f"idem:{owner_id}/{key}"

    async def try_claim(self, owner_id: str, key: str, ttl_seconds: int) -> dict[str, Any] | None:
        valkey_key = self._key(owner_id, key)
        # SET NX EX ttl atomically — returns OK on first write, None on
        # collision. GLIDE exposes this via the ``set`` call's
        # ``conditional_set`` / ``expiry`` options.
        from glide import ConditionalChange, ExpirySet, ExpiryType

        ok = await self._client.set(
            valkey_key,
            self._SENTINEL,
            conditional_set=ConditionalChange.ONLY_IF_DOES_NOT_EXIST,
            expiry=ExpirySet(ExpiryType.SEC, max(1, ttl_seconds)),
        )
        if ok:
            return None
        # Collision — read the cached body. If it's still the sentinel,
        # a concurrent request hasn't finished yet; return an empty
        # cached body so the caller does not duplicate the write.
        raw = await self._client.get(valkey_key)
        if raw is None:
            # Lost a race with TTL expiry — fall through as a fresh request.
            return None
        body = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if body == self._SENTINEL:
            return {}
        try:
            return cast("dict[str, Any]", json.loads(body))
        except json.JSONDecodeError:
            logger.warning("Idempotency cache hit but body is malformed; treating as fresh")
            return None

    async def record(self, owner_id: str, key: str, response: dict[str, Any], ttl_seconds: int) -> None:
        from glide import ExpirySet, ExpiryType

        valkey_key = self._key(owner_id, key)
        await self._client.set(
            valkey_key,
            json.dumps(response),
            expiry=ExpirySet(ExpiryType.SEC, max(1, ttl_seconds)),
        )


# Cache window must be substantially longer than the client's worst
# retry envelope (~127s with the defaults) so a duplicate that arrives
# at the tail of the retry loop still hits the cache. 600s (10 min) is
# the client/server convention; bumping it lower invites duplicates,
# higher just costs more memory.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 600


__all__ = [
    "IdempotencyStorage",
    "InMemoryIdempotencyStorage",
    "ValkeyIdempotencyStorage",
    "DEFAULT_IDEMPOTENCY_TTL_SECONDS",
]
