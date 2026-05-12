"""Tests for the RFC 8628 device-authorization-grant storage layer."""

from datetime import timedelta

import pytest

from pulsar_relay.auth.device_flow import (
    InMemoryDeviceCodeStorage,
    generate_user_code,
)


def test_user_code_format():
    code = generate_user_code()
    assert len(code) == 9  # XXXX-XXXX
    assert code[4] == "-"
    # No vowels or 0/1 — the alphabet excludes them.
    forbidden = set("AEIOU01")
    assert not (set(code) & forbidden)


@pytest.mark.anyio
async def test_create_lookup_round_trip():
    storage = InMemoryDeviceCodeStorage()
    record, device_code = await storage.create(
        verification_uri="https://relay/auth/device",
        verification_uri_complete_template="https://relay/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
    )
    assert record.user_code in record.verification_uri_complete

    by_dc = await storage.get_by_device_code(device_code)
    assert by_dc is not None
    assert by_dc.user_code == record.user_code

    by_uc = await storage.get_by_user_code(record.user_code)
    assert by_uc is not None
    assert by_uc.device_code_hash == record.device_code_hash


@pytest.mark.anyio
async def test_approve_flow():
    storage = InMemoryDeviceCodeStorage()
    record, device_code = await storage.create(
        verification_uri="https://relay/auth/device",
        verification_uri_complete_template="https://relay/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
    )

    approved = await storage.approve(record.user_code, "user-42")
    assert approved is not None
    assert approved.status == "approved"
    assert approved.user_id == "user-42"

    # consume() returns the approved record and makes it single-use.
    consumed = await storage.consume(device_code)
    assert consumed is not None
    assert consumed.status == "approved"

    again = await storage.consume(device_code)
    assert again is None


@pytest.mark.anyio
async def test_approve_after_expiry_marks_expired():
    storage = InMemoryDeviceCodeStorage()
    record, _ = await storage.create(
        verification_uri="https://relay/auth/device",
        verification_uri_complete_template="https://relay/auth/device?user_code={user_code}",
        ttl=timedelta(seconds=-1),  # already expired
    )
    result = await storage.approve(record.user_code, "user-42")
    assert result is None
    refreshed = await storage.get_by_user_code(record.user_code)
    assert refreshed.status == "expired"


@pytest.mark.anyio
async def test_deny_flow():
    storage = InMemoryDeviceCodeStorage()
    record, _ = await storage.create(
        verification_uri="https://relay/auth/device",
        verification_uri_complete_template="https://relay/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
    )
    denied = await storage.deny(record.user_code)
    assert denied is not None
    assert denied.status == "denied"

    # Re-denying or approving after denial is a no-op (returns None).
    assert await storage.approve(record.user_code, "user-42") is None
    assert await storage.deny(record.user_code) is None


@pytest.mark.anyio
async def test_pair_flag_round_trips_through_storage():
    """Galaxy BYOC bootstrap sets ``pair=True`` on ``/auth/device/code`` to
    request two independent refresh tokens at issuance time. The flag must
    survive the storage layer so the token-poll endpoint can read it back."""
    storage = InMemoryDeviceCodeStorage()
    record, device_code = await storage.create(
        verification_uri="https://relay/auth/device",
        verification_uri_complete_template="https://relay/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
        pair=True,
    )
    assert record.pair is True
    fetched = await storage.get_by_device_code(device_code)
    assert fetched is not None
    assert fetched.pair is True


@pytest.mark.anyio
async def test_pair_flag_defaults_false():
    """Existing callers that don't ask for a pair must keep their single-token
    behaviour; ``pair`` must default to False."""
    storage = InMemoryDeviceCodeStorage()
    record, _ = await storage.create(
        verification_uri="https://relay/auth/device",
        verification_uri_complete_template="https://relay/auth/device?user_code={user_code}",
        ttl=timedelta(minutes=10),
    )
    assert record.pair is False
