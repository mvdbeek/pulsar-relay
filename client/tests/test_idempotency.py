"""Tests for client-side Idempotency-Key generation (Client H#2).

The retry loop in :meth:`RelayTransport.post_message` can re-issue
the same logical write multiple times under server-side 5xx flapping.
The Idempotency-Key UUID must be generated ONCE per logical publish
and re-used across retries — otherwise the server can't dedupe and a
network blip becomes a duplicate write.
"""

from __future__ import annotations

import responses
from pulsar_relay_client import RelayTransport
from pulsar_relay_client.testing import FakeAuthManager


@responses.activate
def test_post_message_sends_idempotency_key() -> None:
    """Single happy-path POST carries an Idempotency-Key header."""
    responses.add(
        responses.POST,
        "https://relay.test/api/v1/messages",
        json={"message_id": "m1", "topic": "t", "timestamp": "2026-01-01T00:00:00Z"},
        status=201,
    )
    transport = RelayTransport(
        "https://relay.test",
        auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
    )
    transport.post_message("t", {"x": 1})

    assert len(responses.calls) == 1
    key = responses.calls[0].request.headers.get("Idempotency-Key")
    assert key is not None
    assert len(key) == 32  # uuid4().hex


@responses.activate
def test_retry_reuses_same_idempotency_key() -> None:
    """A 5xx-then-201 sequence must use the SAME Idempotency-Key on
    both HTTP attempts so the server can dedupe."""
    responses.add(
        responses.POST,
        "https://relay.test/api/v1/messages",
        json={"error": "boom"},
        status=503,
    )
    responses.add(
        responses.POST,
        "https://relay.test/api/v1/messages",
        json={"message_id": "m1", "topic": "t", "timestamp": "2026-01-01T00:00:00Z"},
        status=201,
    )
    # sleep=lambda _: None so the test doesn't actually wait on the
    # exponential-backoff delay.
    transport = RelayTransport(
        "https://relay.test",
        auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
        sleep=lambda _: None,
    )
    transport.post_message("t", {"x": 1})

    assert len(responses.calls) == 2
    keys = [c.request.headers.get("Idempotency-Key") for c in responses.calls]
    assert keys[0] is not None
    assert keys[0] == keys[1], "retry used a different Idempotency-Key"


@responses.activate
def test_two_separate_post_calls_use_distinct_keys() -> None:
    """Idempotency-Key is per logical publish — two unrelated
    ``post_message`` calls must NOT share a key."""
    responses.add(
        responses.POST,
        "https://relay.test/api/v1/messages",
        json={"message_id": "m1", "topic": "t", "timestamp": "2026-01-01T00:00:00Z"},
        status=201,
    )
    responses.add(
        responses.POST,
        "https://relay.test/api/v1/messages",
        json={"message_id": "m2", "topic": "t", "timestamp": "2026-01-01T00:00:01Z"},
        status=201,
    )
    transport = RelayTransport(
        "https://relay.test",
        auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
    )
    transport.post_message("t", {"x": 1})
    transport.post_message("t", {"x": 2})

    keys = [c.request.headers.get("Idempotency-Key") for c in responses.calls]
    assert keys[0] != keys[1]
