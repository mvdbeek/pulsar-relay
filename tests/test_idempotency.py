"""Tests for server-side Idempotency-Key dedupe (Client H#2).

The pulsar-relay-client v1.1 generates one ``Idempotency-Key`` UUID
per logical publish and re-uses it across retry attempts. The server
records the response body for a short window so a duplicate POST
returns the original ``message_id`` instead of writing again.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pulsar_relay.api import messages, topics
from pulsar_relay.auth.dependencies import set_topic_storage, set_user_storage
from pulsar_relay.auth.jwt import create_access_token
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage
from pulsar_relay.core.idempotency import InMemoryIdempotencyStorage
from pulsar_relay.main import app
from pulsar_relay.storage.memory import MemoryStorage


@pytest.fixture
async def client_with_idem(auth_storage):
    """Wire a fresh storage stack PLUS an idempotency cache.

    Each test gets its own InMemoryIdempotencyStorage so dedupe entries
    don't leak across tests.
    """
    storage = MemoryStorage()
    messages.set_storage(storage)
    topics.set_storage(storage)
    topic_storage = InMemoryTopicStorage()
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage
    app.state.idempotency_storage = InMemoryIdempotencyStorage()

    user = await auth_storage.get_user_by_username("user")
    token = create_access_token(user)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.mark.anyio
async def test_idempotency_returns_cached_message_id(client_with_idem) -> None:
    """A duplicate POST with the same Idempotency-Key returns the
    original message_id without writing a second message."""
    idem = "test-key-abc123"

    r1 = await client_with_idem.post(
        "/api/v1/messages",
        json={"topic": "t", "payload": {"x": 1}},
        headers={"Idempotency-Key": idem},
    )
    assert r1.status_code == 201
    original_id = r1.json()["message_id"]

    # Same payload, same key — server returns the cached body.
    r2 = await client_with_idem.post(
        "/api/v1/messages",
        json={"topic": "t", "payload": {"x": 1}},
        headers={"Idempotency-Key": idem},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["message_id"] == original_id

    # And — crucially — the storage only has ONE message, not two.
    storage = messages.get_storage()
    # We need the bearer's user_id to look up storage.
    from tests.conftest import _create_auth_storage  # noqa: F401 — only for type hint

    stored = await storage.get_messages(
        owner_id=(await client_with_idem.get("/auth/me")).json()["user_id"],
        topic="t",
    )
    assert len(stored) == 1


@pytest.mark.anyio
async def test_idempotency_key_scoped_to_owner(client_with_idem, auth_storage) -> None:
    """The same Idempotency-Key under different owners must NOT
    collide — each user gets their own dedupe bucket."""
    idem = "shared-key-xyz"

    r1 = await client_with_idem.post(
        "/api/v1/messages",
        json={"topic": "t", "payload": {"who": "user"}},
        headers={"Idempotency-Key": idem},
    )
    assert r1.status_code == 201

    # Switch to admin token and POST with the SAME idempotency key.
    admin = await auth_storage.get_user_by_username("admin")
    admin_token = create_access_token(admin)
    r2 = await client_with_idem.post(
        "/api/v1/messages",
        json={"topic": "t", "payload": {"who": "admin"}},
        headers={"Authorization": f"Bearer {admin_token}", "Idempotency-Key": idem},
    )
    assert r2.status_code == 201
    # Different message ids because the cache is per (owner_id, key).
    assert r2.json()["message_id"] != r1.json()["message_id"]


@pytest.mark.anyio
async def test_no_idempotency_header_means_no_dedupe(client_with_idem) -> None:
    """Two POSTs without an Idempotency-Key produce two distinct
    writes — the dedupe only kicks in when the header is present."""
    r1 = await client_with_idem.post(
        "/api/v1/messages",
        json={"topic": "t", "payload": {"x": 1}},
    )
    r2 = await client_with_idem.post(
        "/api/v1/messages",
        json={"topic": "t", "payload": {"x": 1}},
    )
    assert r1.json()["message_id"] != r2.json()["message_id"]


@pytest.mark.anyio
async def test_in_memory_idempotency_storage_unit() -> None:
    """Direct unit test of the storage class.

    The first ``try_claim`` returns None (fresh); the second returns
    whatever ``record`` cached.
    """
    s = InMemoryIdempotencyStorage()
    owner, key = "alice", "k1"
    assert await s.try_claim(owner, key, ttl_seconds=60) is None
    # In-flight sentinel: a second claim returns an empty body so the
    # caller does NOT write again.
    assert await s.try_claim(owner, key, ttl_seconds=60) == {}

    await s.record(owner, key, {"message_id": "m1"}, ttl_seconds=60)
    cached = await s.try_claim(owner, key, ttl_seconds=60)
    assert cached == {"message_id": "m1"}
