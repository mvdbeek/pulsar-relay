"""Short-lived OIDC authorization-request state.

A record is created when the relay redirects the user to an upstream IdP and
consumed (single-use) when the IdP redirects back to ``/auth/oidc/{provider}/callback``.
The record holds the PKCE ``code_verifier``, ``nonce``, ``redirect_uri``, the
provider being targeted, and (optionally) a ``device_user_code`` so a single
OIDC sign-in can approve a pending device-flow session.
"""

from __future__ import annotations

import json
import logging
import secrets as secrets_mod
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from pulsar_relay.auth.models import OIDCStateRecord

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def generate_state() -> str:
    return secrets_mod.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets_mod.token_urlsafe(32)


def generate_code_verifier() -> str:
    # PKCE spec: 43-128 chars from the unreserved set. token_urlsafe(64) → ~86 chars.
    return secrets_mod.token_urlsafe(64)


class OIDCStateStorage(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        provider_name: str,
        redirect_uri: str,
        ttl: timedelta,
        next_url: str | None = None,
        device_user_code: str | None = None,
    ) -> OIDCStateRecord:
        pass

    @abstractmethod
    async def consume(self, state: str) -> OIDCStateRecord | None:
        """Atomically remove and return the record. Single-use enforcement."""
        pass


# ---------- in-memory ----------


class InMemoryOIDCStateStorage(OIDCStateStorage):
    def __init__(self) -> None:
        self._records: dict[str, OIDCStateRecord] = {}

    async def create(
        self,
        *,
        provider_name: str,
        redirect_uri: str,
        ttl: timedelta,
        next_url: str | None = None,
        device_user_code: str | None = None,
    ) -> OIDCStateRecord:
        now = _utcnow()
        record = OIDCStateRecord(
            state=generate_state(),
            provider_name=provider_name,
            code_verifier=generate_code_verifier(),
            nonce=generate_nonce(),
            redirect_uri=redirect_uri,
            next_url=next_url,
            device_user_code=device_user_code,
            issued_at=now,
            expires_at=now + ttl,
        )
        self._records[record.state] = record
        return record

    async def consume(self, state: str) -> OIDCStateRecord | None:
        record = self._records.pop(state, None)
        if record is None:
            return None
        if record.expires_at <= _utcnow():
            return None
        return record


# ---------- Valkey ----------


class ValkeyOIDCStateStorage(OIDCStateStorage):
    """Layout: ``oidc:state:{state}`` → JSON blob, with TTL set on the key."""

    def __init__(self, client) -> None:
        self._client = client

    @staticmethod
    def _key(state: str) -> str:
        return f"oidc:state:{state}"

    async def create(
        self,
        *,
        provider_name: str,
        redirect_uri: str,
        ttl: timedelta,
        next_url: str | None = None,
        device_user_code: str | None = None,
    ) -> OIDCStateRecord:
        now = _utcnow()
        record = OIDCStateRecord(
            state=generate_state(),
            provider_name=provider_name,
            code_verifier=generate_code_verifier(),
            nonce=generate_nonce(),
            redirect_uri=redirect_uri,
            next_url=next_url,
            device_user_code=device_user_code,
            issued_at=now,
            expires_at=now + ttl,
        )
        await self._client.set(self._key(record.state), record.model_dump_json())
        await self._client.expire(self._key(record.state), max(int(ttl.total_seconds()), 60))
        return record

    async def consume(self, state: str) -> OIDCStateRecord | None:
        key = self._key(state)
        raw = await self._client.get(key)
        if not raw:
            return None
        await self._client.delete([key])
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except json.JSONDecodeError:
            return None
        record = OIDCStateRecord(**data)
        if record.expires_at <= _utcnow():
            return None
        return record


__all__ = [
    "OIDCStateStorage",
    "InMemoryOIDCStateStorage",
    "ValkeyOIDCStateStorage",
    "generate_state",
    "generate_nonce",
    "generate_code_verifier",
]
