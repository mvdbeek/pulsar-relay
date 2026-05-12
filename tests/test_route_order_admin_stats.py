"""Regression test for the topic-stats admin-route order.

``GET /api/v1/topics/stats`` is gated by ``require_permission("admin")``.
The route's declaration order matters: if it is declared *after*
``GET /api/v1/topics/{topic_name}``, FastAPI's path matcher resolves
``/stats`` to ``get_topic(topic_name="stats")`` — a non-admin endpoint —
and the admin permission check never runs.

This test asserts a non-admin token receives **403** (the permission
check fired) and not 404 (route shadowed). See security review API H#6.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pulsar_relay.api import messages, topics
from pulsar_relay.auth.dependencies import set_topic_storage, set_user_storage
from pulsar_relay.auth.jwt import create_access_token
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage
from pulsar_relay.main import app
from pulsar_relay.storage.memory import MemoryStorage


@pytest.fixture
async def non_admin_client(auth_storage):
    storage = MemoryStorage()
    messages.set_storage(storage)
    topics.set_storage(storage)

    topic_storage = InMemoryTopicStorage()
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    # The "user" fixture in conftest creates a non-admin with read+write.
    user = await auth_storage.get_user_by_username("user")
    assert "admin" not in user.permissions, "fixture invariant: 'user' is non-admin"
    token = create_access_token(user)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac
    await storage.clear()


@pytest.mark.anyio
async def test_non_admin_stats_returns_403(non_admin_client):
    """Non-admin hitting /api/v1/topics/stats must get 403.

    If this test fails with 404, the ``/stats`` route is shadowed by
    ``/{topic_name}`` (which would 404 because no topic named "stats"
    exists). If it fails with 200, the admin dependency is missing.
    """
    resp = await non_admin_client.get("/api/v1/topics/stats")
    assert resp.status_code == 403, f"expected 403 (permission denied), got {resp.status_code}: {resp.text}"
