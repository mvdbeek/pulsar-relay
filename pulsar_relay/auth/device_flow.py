"""Device-authorization-grant (RFC 8628) storage.

A daemon (no browser) calls ``POST /auth/device/code`` and receives a
``device_code`` plus a short ``user_code``. The daemon polls
``POST /auth/device/token`` until an operator approves the ``user_code``
in a browser, where the request is bound to one of the configured OIDC
providers.

The wire ``device_code`` is opaque random; only its sha256 is persisted.
"""

from __future__ import annotations

import hashlib
import logging
import secrets as secrets_mod
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from pulsar_relay.auth.models import DeviceCode

logger = logging.getLogger(__name__)


# Ambiguity-free alphabet (omits 0/O/1/I/L plus vowels A/E/I/O/U to also
# avoid accidentally spelling words). 20 symbols × 8 chars ≈ 35 bits entropy.
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"
_USER_CODE_LEN = 8
_DEVICE_CODE_BYTES = 32


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _hash_device_code(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def generate_user_code() -> str:
    """Eight chars from a 20-symbol alphabet, formatted ``XXXX-XXXX``."""
    raw = "".join(secrets_mod.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def generate_device_code() -> str:
    return secrets_mod.token_urlsafe(_DEVICE_CODE_BYTES)


class DeviceCodeStorage(ABC):
    """Abstract base class for device-code storage."""

    @abstractmethod
    async def create(
        self,
        *,
        verification_uri: str,
        verification_uri_complete_template: str,
        ttl: timedelta,
        interval: int = 5,
        client_hint: str | None = None,
    ) -> tuple[DeviceCode, str]:
        """Issue a new device-code session.

        Returns the persisted record (containing the *hashed* device code)
        and the plaintext device code the daemon must hold.
        """
        pass

    @abstractmethod
    async def get_by_device_code(self, device_code: str) -> DeviceCode | None:
        """Look up a session by the plaintext device_code (hashes internally)."""
        pass

    @abstractmethod
    async def get_by_user_code(self, user_code: str) -> DeviceCode | None:
        pass

    @abstractmethod
    async def update(self, record: DeviceCode) -> None:
        """Persist mutations on the record."""
        pass

    @abstractmethod
    async def approve(self, user_code: str, user_id: str) -> DeviceCode | None:
        """Mark the session approved; returns the updated record (or None)."""
        pass

    @abstractmethod
    async def deny(self, user_code: str) -> DeviceCode | None:
        pass

    @abstractmethod
    async def consume(self, device_code: str) -> DeviceCode | None:
        """Atomically delete the record and return its last state.

        Used after the daemon successfully exchanges device_code for tokens
        — single-use enforcement.
        """
        pass


# ---------- in-memory ----------


class InMemoryDeviceCodeStorage(DeviceCodeStorage):
    def __init__(self) -> None:
        # device_code_hash -> DeviceCode
        self._records: dict[str, DeviceCode] = {}
        self._user_code_index: dict[str, str] = {}

    def _build(
        self,
        *,
        verification_uri: str,
        verification_uri_complete_template: str,
        ttl: timedelta,
        interval: int,
        client_hint: str | None,
    ) -> tuple[DeviceCode, str]:
        # Avoid user_code collisions with active sessions.
        for _ in range(10):
            user_code = generate_user_code()
            if user_code not in self._user_code_index:
                break
        else:  # pragma: no cover - astronomically unlikely
            raise RuntimeError("could not allocate unique user_code")

        device_code = generate_device_code()
        now = _utcnow()
        record = DeviceCode(
            device_code_hash=_hash_device_code(device_code),
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete_template.format(user_code=user_code),
            issued_at=now,
            expires_at=now + ttl,
            interval=interval,
            client_hint=client_hint,
        )
        return record, device_code

    async def create(
        self,
        *,
        verification_uri: str,
        verification_uri_complete_template: str,
        ttl: timedelta,
        interval: int = 5,
        client_hint: str | None = None,
    ) -> tuple[DeviceCode, str]:
        record, device_code = self._build(
            verification_uri=verification_uri,
            verification_uri_complete_template=verification_uri_complete_template,
            ttl=ttl,
            interval=interval,
            client_hint=client_hint,
        )
        self._records[record.device_code_hash] = record
        self._user_code_index[record.user_code] = record.device_code_hash
        return record, device_code

    async def get_by_device_code(self, device_code: str) -> DeviceCode | None:
        return self._records.get(_hash_device_code(device_code))

    async def get_by_user_code(self, user_code: str) -> DeviceCode | None:
        h = self._user_code_index.get(user_code.upper())
        if h is None:
            return None
        return self._records.get(h)

    async def update(self, record: DeviceCode) -> None:
        self._records[record.device_code_hash] = record

    async def approve(self, user_code: str, user_id: str) -> DeviceCode | None:
        record = await self.get_by_user_code(user_code)
        if record is None or record.status != "pending":
            return None
        if record.expires_at <= _utcnow():
            record.status = "expired"
            await self.update(record)
            return None
        record.status = "approved"
        record.user_id = user_id
        await self.update(record)
        return record

    async def deny(self, user_code: str) -> DeviceCode | None:
        record = await self.get_by_user_code(user_code)
        if record is None or record.status != "pending":
            return None
        record.status = "denied"
        await self.update(record)
        return record

    async def consume(self, device_code: str) -> DeviceCode | None:
        record = self._records.pop(_hash_device_code(device_code), None)
        if record is not None:
            self._user_code_index.pop(record.user_code, None)
        return record


# ---------- Valkey ----------


class ValkeyDeviceCodeStorage(DeviceCodeStorage):
    """Valkey-backed device-code storage.

    Layout:
      - ``device:tok:{device_code_hash}`` (hash) — the record.
      - ``device:uc:{user_code}`` (string) — points to the device_code_hash.

    Both keys carry TTL set to slightly past ``expires_at``.
    """

    def __init__(self, client) -> None:
        self._client = client

    @staticmethod
    def _record_key(device_code_hash: str) -> str:
        return f"device:tok:{device_code_hash}"

    @staticmethod
    def _user_code_key(user_code: str) -> str:
        return f"device:uc:{user_code.upper()}"

    @staticmethod
    def _serialize(record: DeviceCode) -> dict[str, str]:
        return {
            "device_code_hash": record.device_code_hash,
            "user_code": record.user_code,
            "verification_uri": record.verification_uri,
            "verification_uri_complete": record.verification_uri_complete,
            "issued_at": record.issued_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "interval": str(record.interval),
            "last_polled_at": record.last_polled_at.isoformat() if record.last_polled_at else "",
            "status": record.status,
            "user_id": record.user_id or "",
            "client_hint": record.client_hint or "",
        }

    @staticmethod
    def _deserialize(raw: dict[bytes, bytes]) -> DeviceCode:
        d = {k.decode("utf-8"): v.decode("utf-8") for k, v in raw.items()}
        last_polled = d.get("last_polled_at") or None
        return DeviceCode(
            device_code_hash=d["device_code_hash"],
            user_code=d["user_code"],
            verification_uri=d["verification_uri"],
            verification_uri_complete=d["verification_uri_complete"],
            issued_at=datetime.fromisoformat(d["issued_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
            interval=int(d.get("interval", "5")),
            last_polled_at=datetime.fromisoformat(last_polled) if last_polled else None,
            status=d.get("status", "pending"),  # type: ignore[arg-type]
            user_id=d.get("user_id") or None,
            client_hint=d.get("client_hint") or None,
        )

    async def _store(self, record: DeviceCode) -> None:
        key = self._record_key(record.device_code_hash)
        await self._client.hset(key, self._serialize(record))
        ttl_seconds = max(int((record.expires_at - _utcnow()).total_seconds()) + 60, 60)
        await self._client.expire(key, ttl_seconds)

    async def create(
        self,
        *,
        verification_uri: str,
        verification_uri_complete_template: str,
        ttl: timedelta,
        interval: int = 5,
        client_hint: str | None = None,
    ) -> tuple[DeviceCode, str]:
        # Hand off the heavy lifting; reuse the in-memory builder for the
        # collision-avoidance loop (it only checks local state, which is fine
        # for a 35-bit code).
        device_code = generate_device_code()
        for _ in range(10):
            user_code = generate_user_code()
            uc_key = self._user_code_key(user_code)
            # SET NX guarantees the user_code is unique.
            try:
                claimed = await self._client.set(
                    uc_key,
                    _hash_device_code(device_code),
                    conditional_set="only_if_does_not_exist",
                )
            except TypeError:  # client signature differences
                claimed = await self._client.set(uc_key, _hash_device_code(device_code))
            if claimed:
                break
        else:  # pragma: no cover
            raise RuntimeError("could not allocate unique user_code")

        ttl_seconds = max(int(ttl.total_seconds()) + 60, 60)
        await self._client.expire(uc_key, ttl_seconds)

        now = _utcnow()
        record = DeviceCode(
            device_code_hash=_hash_device_code(device_code),
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete_template.format(user_code=user_code),
            issued_at=now,
            expires_at=now + ttl,
            interval=interval,
            client_hint=client_hint,
        )
        await self._store(record)
        return record, device_code

    async def get_by_device_code(self, device_code: str) -> DeviceCode | None:
        raw = await self._client.hgetall(self._record_key(_hash_device_code(device_code)))
        if not raw:
            return None
        return self._deserialize(raw)

    async def get_by_user_code(self, user_code: str) -> DeviceCode | None:
        hash_bytes = await self._client.get(self._user_code_key(user_code))
        if not hash_bytes:
            return None
        raw = await self._client.hgetall(self._record_key(hash_bytes.decode("utf-8")))
        if not raw:
            return None
        return self._deserialize(raw)

    async def update(self, record: DeviceCode) -> None:
        await self._store(record)

    async def approve(self, user_code: str, user_id: str) -> DeviceCode | None:
        record = await self.get_by_user_code(user_code)
        if record is None or record.status != "pending":
            return None
        if record.expires_at <= _utcnow():
            record.status = "expired"
            await self._store(record)
            return None
        record.status = "approved"
        record.user_id = user_id
        await self._store(record)
        return record

    async def deny(self, user_code: str) -> DeviceCode | None:
        record = await self.get_by_user_code(user_code)
        if record is None or record.status != "pending":
            return None
        record.status = "denied"
        await self._store(record)
        return record

    async def consume(self, device_code: str) -> DeviceCode | None:
        record = await self.get_by_device_code(device_code)
        if record is None:
            return None
        await self._client.delete([self._record_key(record.device_code_hash), self._user_code_key(record.user_code)])
        return record


__all__ = [
    "DeviceCodeStorage",
    "InMemoryDeviceCodeStorage",
    "ValkeyDeviceCodeStorage",
    "generate_user_code",
    "generate_device_code",
]
