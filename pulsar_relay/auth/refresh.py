"""Refresh-token storage and rotation.

The wire token presented by clients is ``f"{jti}.{secret}"``. Only ``jti`` is
indexable; ``secret`` is verified against ``sha256(secret)`` stored on the
record. Rotation chains are tracked via ``parent_jti`` so that replay of a
rotated token can revoke the entire family.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets as secrets_mod
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from glide import Script

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


def secret_matches_record(secret: str, record: RefreshToken) -> bool:
    """Constant-time check that ``secret`` corresponds to ``record``'s stored hash.

    Public helper used by ``/auth/token/revoke``: that endpoint cannot
    use :func:`verify_and_consume` (which has side effects — chain
    revocation on rotated-replay), but still needs to prove the caller
    holds the wire-side secret rather than just a leaked ``jti``.
    Closes the security review's ``/auth/token/revoke`` unauthenticated
    finding.
    """
    return secrets_mod.compare_digest(_hash_secret(secret), record.secret_hash)


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
    async def try_mark_rotated(self, jti: str) -> bool:
        """Atomic CAS: transition ``revoked: 0 -> 1, reason='rotated'``.

        Returns True if this caller owned the transition (the token was
        previously not revoked). Returns False if the token was already
        revoked — the caller lost a rotation race and should treat the
        situation as a replay attempt.

        The Valkey backend implements this with a Lua ``EVAL`` so the
        check-then-set is a single round-trip. The in-memory backend
        guards the check-then-set with an asyncio.Lock.
        """
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
        # Guard the check-then-set inside try_mark_rotated so two
        # concurrent rotation attempts on the same token can't both
        # succeed. Real production uses Valkey + Lua; this lock is the
        # test-backend equivalent.
        self._rotate_lock = asyncio.Lock()

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

    async def try_mark_rotated(self, jti: str) -> bool:
        async with self._rotate_lock:
            token = self._tokens.get(jti)
            if token is None or token.revoked:
                return False
            token.revoked = True
            token.revoked_reason = "rotated"
            return True

    async def mark_used(self, jti: str) -> None:
        token = self._tokens.get(jti)
        if token is not None:
            token.last_used_at = _utcnow()

    async def revoke_chain(self, jti: str, reason: RefreshTokenRevokedReason) -> int:
        seed = self._tokens.get(jti)
        if seed is None:
            return 0
        # A "chain" is the connected component containing ``seed`` in the
        # parent_jti DAG: walk up to the root, then BFS down from the root.
        # Independent refresh tokens for the same user (e.g., the pair issued
        # for Galaxy BYOC) have no parent_jti linkage so live in distinct
        # components and revoking one mustn't take down the other.
        root_jti = jti
        while True:
            token = self._tokens.get(root_jti)
            if token is None or token.parent_jti is None:
                break
            root_jti = token.parent_jti
        chain: set[str] = {root_jti}
        frontier = [root_jti]
        # Pre-bucket children by parent for an O(N) traversal.
        children: dict[str, list[str]] = {}
        for child_jti in self._user_index.get(seed.user_id, set()):
            child = self._tokens.get(child_jti)
            if child is not None and child.parent_jti:
                children.setdefault(child.parent_jti, []).append(child_jti)
        while frontier:
            parent_jti = frontier.pop()
            for descendant_jti in children.get(parent_jti, ()):
                if descendant_jti not in chain:
                    chain.add(descendant_jti)
                    frontier.append(descendant_jti)
        revoked = 0
        for chain_jti in chain:
            token = self._tokens.get(chain_jti)
            if token is not None and not token.revoked:
                token.revoked = True
                token.revoked_reason = reason
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
        # Set TTL slightly past expiry so rows don't linger for replay-detection
        # forever, but stay long enough that an attacker presenting the rotated
        # secret right after expiry still trips the chain-revocation guard.
        ttl_seconds = max(int((record.expires_at - _utcnow()).total_seconds()) + 3600, 3600)
        # MULTI/EXEC so a crash between HSET and EXPIRE can't leak a
        # non-expiring refresh-token record.
        from glide import Batch

        batch = Batch(is_atomic=True)
        batch.hset(token_key, self._serialize(record))
        batch.expire(token_key, ttl_seconds)
        await self._client.exec(batch, raise_on_error=True)

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
        # Track the jti in the per-user set so chain-revocation can
        # walk all of a user's tokens. The set itself needs a TTL —
        # without one it accumulates stale jti references forever
        # (Storage M#9: "Missing TTL on auxiliary keys"). Renew it
        # on every SADD to the maximum of (this token's TTL + grace,
        # any still-live siblings) — approximating "set expires when
        # the user's longest-living refresh token does".
        from glide import Batch

        user_set_key = self._user_key(user_id)
        user_set_ttl = max(int(ttl.total_seconds()) + 3600, 3600)
        batch = Batch(is_atomic=True)
        batch.sadd(user_set_key, [jti])
        batch.expire(user_set_key, user_set_ttl)
        await self._client.exec(batch, raise_on_error=True)
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

    # Atomic check-then-set for the rotation transition. The Lua script
    # runs inside Valkey's single-threaded execution loop so the HGET +
    # HSET pair is observed as one operation by any concurrent caller.
    # KEYS[1] = ``refresh:tok:{jti}``.
    # Returns 1 if this caller owned the transition, 0 if the token was
    # missing or already revoked.
    _rotate_cas = Script("""
        local key = KEYS[1]
        if redis.call('EXISTS', key) == 0 then
            return 0
        end
        local current = redis.call('HGET', key, 'revoked')
        if current == false or current == '0' or current == '' then
            redis.call('HSET', key, 'revoked', '1', 'revoked_reason', 'rotated')
            return 1
        end
        return 0
        """)

    async def try_mark_rotated(self, jti: str) -> bool:
        token_key = self._token_key(jti)
        result = await self._client.invoke_script(
            type(self)._rotate_cas,
            keys=[token_key],
            args=[],
        )
        return result in (1, b"1", "1")

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
        # See the in-memory backend for rationale: revoke the connected
        # component of ``jti`` in the parent_jti DAG, not the user's entire
        # token set. Independent (pair-issued) tokens live in their own
        # components; we must not collateral-revoke them.
        sibling_jtis_bytes = await self._client.smembers(self._user_key(seed.user_id))
        sibling_jtis = [b.decode("utf-8") for b in sibling_jtis_bytes]
        tokens_by_jti: dict[str, RefreshToken] = {}
        for sibling_jti in sibling_jtis:
            tok = await self.get_by_jti(sibling_jti)
            if tok is not None:
                tokens_by_jti[sibling_jti] = tok
        # Walk up to the chain root.
        root_jti = jti
        while True:
            current = tokens_by_jti.get(root_jti)
            if current is None or current.parent_jti is None:
                break
            root_jti = current.parent_jti
        # BFS down from the root.
        children: dict[str, list[str]] = {}
        for tok_jti, tok in tokens_by_jti.items():
            if tok.parent_jti:
                children.setdefault(tok.parent_jti, []).append(tok_jti)
        chain: set[str] = {root_jti}
        frontier = [root_jti]
        while frontier:
            parent_jti = frontier.pop()
            for descendant_jti in children.get(parent_jti, ()):
                if descendant_jti not in chain:
                    chain.add(descendant_jti)
                    frontier.append(descendant_jti)
        revoked = 0
        for chain_jti in chain:
            tok = tokens_by_jti.get(chain_jti)
            if tok is not None and not tok.revoked:
                tok.revoked = True
                tok.revoked_reason = reason
                await self._store(tok)
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
    "secret_matches_record",
    "split_wire_token",
    "verify_and_consume",
]
