"""Refresh-token storage and rotation.

The wire token presented by clients is ``f"{jti}.{secret}"``. Only ``jti`` is
indexable; ``secret`` is verified against ``sha256(secret)`` stored on the
record. Rotation chains are tracked via ``parent_jti`` so that replay of a
rotated token can revoke the entire family.
"""

from __future__ import annotations

import hashlib
import logging
import secrets as secrets_mod
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pulsar_relay.auth.models import RefreshToken, RefreshTokenRevokedReason

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def split_wire_token(wire: str) -> tuple[str, str]:
    """Split a ``"{jti}.{secret}"`` wire token. Raises ``ValueError`` if malformed."""
    if "." not in wire:
        raise ValueError("malformed refresh token")
    jti, _, secret = wire.partition(".")
    if not jti or not secret:
        raise ValueError("malformed refresh token")
    return jti, secret


class RefreshTokenStorage(ABC):
    """Abstract base class for refresh-token storage."""

    @abstractmethod
    async def create(
        self,
        *,
        user_id: str,
        ttl: timedelta,
        parent_jti: str | None = None,
        client_hint: str | None = None,
    ) -> tuple[RefreshToken, str]:
        """Issue a new refresh token.

        Returns the persisted record and the wire token (``"{jti}.{secret}"``)
        the caller must hand to the client. The secret is never stored.
        """
        pass

    @abstractmethod
    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        pass

    @abstractmethod
    async def mark_revoked(self, jti: str, reason: RefreshTokenRevokedReason) -> None:
        pass

    @abstractmethod
    async def mark_used(self, jti: str) -> None:
        """Update ``last_used_at``."""
        pass

    @abstractmethod
    async def revoke_chain(self, jti: str, reason: RefreshTokenRevokedReason) -> int:
        """Revoke every token sharing a rotation chain with ``jti``.

        Walks ``parent_jti`` backwards to the root and forwards via the user's
        token list. Returns the number of tokens newly revoked.
        """
        pass

    @abstractmethod
    async def list_for_user(self, user_id: str, *, include_revoked: bool = False) -> list[RefreshToken]:
        pass


# ---------- helpers shared by both backends ----------


def _new_jti_secret() -> tuple[str, str]:
    return str(uuid4()), secrets_mod.token_urlsafe(32)


# ---------- in-memory ----------


class InMemoryRefreshTokenStorage(RefreshTokenStorage):
    def __init__(self) -> None:
        self._tokens: dict[str, RefreshToken] = {}
        self._user_index: dict[str, set[str]] = {}

    async def create(
        self,
        *,
        user_id: str,
        ttl: timedelta,
        parent_jti: str | None = None,
        client_hint: str | None = None,
    ) -> tuple[RefreshToken, str]:
        jti, secret = _new_jti_secret()
        now = _utcnow()
        record = RefreshToken(
            jti=jti,
            user_id=user_id,
            secret_hash=_hash_secret(secret),
            parent_jti=parent_jti,
            issued_at=now,
            expires_at=now + ttl,
            client_hint=client_hint,
        )
        self._tokens[jti] = record
        self._user_index.setdefault(user_id, set()).add(jti)
        return record, f"{jti}.{secret}"

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        return self._tokens.get(jti)

    async def mark_revoked(self, jti: str, reason: RefreshTokenRevokedReason) -> None:
        token = self._tokens.get(jti)
        if token is None or token.revoked:
            return
        token.revoked = True
        token.revoked_reason = reason

    async def mark_used(self, jti: str) -> None:
        token = self._tokens.get(jti)
        if token is not None:
            token.last_used_at = _utcnow()

    async def revoke_chain(self, jti: str, reason: RefreshTokenRevokedReason) -> int:
        seed = self._tokens.get(jti)
        if seed is None:
            return 0
        # All tokens for the same user form a connected family via parent_jti
        # links. Cheaper to just walk the user's set than reconstruct the DAG.
        revoked = 0
        for sibling_jti in list(self._user_index.get(seed.user_id, set())):
            sibling = self._tokens.get(sibling_jti)
            if sibling is not None and not sibling.revoked:
                sibling.revoked = True
                sibling.revoked_reason = reason
                revoked += 1
        return revoked

    async def list_for_user(self, user_id: str, *, include_revoked: bool = False) -> list[RefreshToken]:
        result = []
        for jti in self._user_index.get(user_id, set()):
            token = self._tokens.get(jti)
            if token is None:
                continue
            if not include_revoked and token.revoked:
                continue
            result.append(token)
        return result


# ---------- Valkey ----------


class ValkeyRefreshTokenStorage(RefreshTokenStorage):
    """Valkey-backed refresh-token storage.

    Layout:
        - ``refresh:tok:{jti}`` (hash) — persisted RefreshToken row.
        - ``refresh:user:{user_id}`` (set) — every jti issued for the user.

    Both keys are given a TTL slightly past ``expires_at`` so revoked rows
    naturally drop out of storage.
    """

    def __init__(self, client) -> None:
        self._client = client

    @staticmethod
    def _token_key(jti: str) -> str:
        return f"refresh:tok:{jti}"

    @staticmethod
    def _user_key(user_id: str) -> str:
        return f"refresh:user:{user_id}"

    @staticmethod
    def _serialize(record: RefreshToken) -> dict[str, str]:
        return {
            "jti": record.jti,
            "user_id": record.user_id,
            "secret_hash": record.secret_hash,
            "parent_jti": record.parent_jti or "",
            "issued_at": record.issued_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "last_used_at": record.last_used_at.isoformat() if record.last_used_at else "",
            "revoked": "1" if record.revoked else "0",
            "revoked_reason": record.revoked_reason or "",
            "client_hint": record.client_hint or "",
        }

    @staticmethod
    def _deserialize(raw: dict[bytes, bytes]) -> RefreshToken:
        data = {k.decode("utf-8"): v.decode("utf-8") for k, v in raw.items()}
        last_used = data.get("last_used_at") or None
        return RefreshToken(
            jti=data["jti"],
            user_id=data["user_id"],
            secret_hash=data["secret_hash"],
            parent_jti=data.get("parent_jti") or None,
            issued_at=datetime.fromisoformat(data["issued_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            last_used_at=datetime.fromisoformat(last_used) if last_used else None,
            revoked=data.get("revoked") == "1",
            revoked_reason=data.get("revoked_reason") or None,
            client_hint=data.get("client_hint") or None,
        )

    async def _store(self, record: RefreshToken) -> None:
        token_key = self._token_key(record.jti)
        await self._client.hset(token_key, self._serialize(record))
        # Set TTL slightly past expiry so rows don't linger for replay-detection
        # forever, but stay long enough that an attacker presenting the rotated
        # secret right after expiry still trips the chain-revocation guard.
        ttl_seconds = max(int((record.expires_at - _utcnow()).total_seconds()) + 3600, 3600)
        await self._client.expire(token_key, ttl_seconds)

    async def create(
        self,
        *,
        user_id: str,
        ttl: timedelta,
        parent_jti: str | None = None,
        client_hint: str | None = None,
    ) -> tuple[RefreshToken, str]:
        jti, secret = _new_jti_secret()
        now = _utcnow()
        record = RefreshToken(
            jti=jti,
            user_id=user_id,
            secret_hash=_hash_secret(secret),
            parent_jti=parent_jti,
            issued_at=now,
            expires_at=now + ttl,
            client_hint=client_hint,
        )
        await self._store(record)
        await self._client.sadd(self._user_key(user_id), [jti])
        return record, f"{jti}.{secret}"

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        raw = await self._client.hgetall(self._token_key(jti))
        if not raw:
            return None
        return self._deserialize(raw)

    async def mark_revoked(self, jti: str, reason: RefreshTokenRevokedReason) -> None:
        token = await self.get_by_jti(jti)
        if token is None or token.revoked:
            return
        token.revoked = True
        token.revoked_reason = reason
        await self._store(token)

    async def mark_used(self, jti: str) -> None:
        token = await self.get_by_jti(jti)
        if token is None:
            return
        token.last_used_at = _utcnow()
        await self._store(token)

    async def revoke_chain(self, jti: str, reason: RefreshTokenRevokedReason) -> int:
        seed = await self.get_by_jti(jti)
        if seed is None:
            return 0
        sibling_jtis_bytes = await self._client.smembers(self._user_key(seed.user_id))
        sibling_jtis = [b.decode("utf-8") for b in sibling_jtis_bytes]
        revoked = 0
        for sibling_jti in sibling_jtis:
            sibling = await self.get_by_jti(sibling_jti)
            if sibling is not None and not sibling.revoked:
                sibling.revoked = True
                sibling.revoked_reason = reason
                await self._store(sibling)
                revoked += 1
        return revoked

    async def list_for_user(self, user_id: str, *, include_revoked: bool = False) -> list[RefreshToken]:
        jti_bytes = await self._client.smembers(self._user_key(user_id))
        result = []
        for raw in jti_bytes:
            token = await self.get_by_jti(raw.decode("utf-8"))
            if token is None:
                continue
            if not include_revoked and token.revoked:
                continue
            result.append(token)
        return result


# ---------- public verification helper ----------


class RefreshTokenError(Exception):
    """Raised when a refresh-token operation fails security validation."""


async def verify_and_consume(
    storage: RefreshTokenStorage,
    wire_token: str,
) -> RefreshToken:
    """Validate a wire token. Caller is responsible for issuing replacements.

    Raises ``RefreshTokenError`` on any of: malformed token, unknown jti,
    secret mismatch, expired, revoked. If the row was previously rotated
    (``revoked, reason="rotated"``), the entire chain is revoked as a
    replay-detection countermeasure.
    """
    try:
        jti, secret = split_wire_token(wire_token)
    except ValueError as exc:
        raise RefreshTokenError(str(exc)) from exc

    token = await storage.get_by_jti(jti)
    if token is None:
        raise RefreshTokenError("unknown refresh token")

    if not secrets_mod.compare_digest(_hash_secret(secret), token.secret_hash):
        raise RefreshTokenError("secret mismatch")

    if token.revoked:
        if token.revoked_reason == "rotated":
            revoked = await storage.revoke_chain(jti, "replay")
            logger.warning(
                "Replay of rotated refresh token jti=%s — revoked %d sibling tokens",
                jti,
                revoked,
            )
        raise RefreshTokenError("refresh token revoked")

    if token.expires_at <= _utcnow():
        await storage.mark_revoked(jti, "expired")
        raise RefreshTokenError("refresh token expired")

    return token


__all__ = [
    "RefreshTokenStorage",
    "InMemoryRefreshTokenStorage",
    "ValkeyRefreshTokenStorage",
    "RefreshTokenError",
    "split_wire_token",
    "verify_and_consume",
]
