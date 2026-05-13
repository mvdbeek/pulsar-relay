"""End-to-end tests for per-user topic namespacing (API H#5).

Phase 3c moves topic storage from a flat namespace to ``(owner_id,
topic_name)`` so two users can each have a topic called ``"jobs"``
without colliding. These tests exercise the full API path:

1. Two users each create ``"jobs"`` — both succeed.
2. User A's published messages do NOT appear in user B's poll of
   ``"jobs"``.
3. Migration: pre-Phase-3c flat keys cause the startup guard to refuse
   to boot unless the escape hatch is set.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pulsar_relay.api import messages, topics
from pulsar_relay.auth.dependencies import set_topic_storage, set_user_storage
from pulsar_relay.auth.jwt import create_access_token
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage, scan_for_legacy_keys
from pulsar_relay.main import app
from pulsar_relay.storage.memory import MemoryStorage


@pytest.fixture
async def two_clients(auth_storage):
    """Wire two authenticated clients (admin + regular user) on a fresh
    storage stack."""
    storage = MemoryStorage()
    messages.set_storage(storage)
    topics.set_storage(storage)
    topic_storage = InMemoryTopicStorage()
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    user = await auth_storage.get_user_by_username("user")
    admin = await auth_storage.get_user_by_username("admin")
    user_tok = create_access_token(user)
    admin_tok = create_access_token(admin)

    transport = ASGITransport(app=app)
    user_client = AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {user_tok}"},
    )
    admin_client = AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    async with user_client as user_c, admin_client as admin_c:
        yield user_c, admin_c, user, admin


@pytest.mark.anyio
async def test_two_users_both_create_jobs_topic(two_clients) -> None:
    """The squat is gone: both users get their own ``"jobs"``."""
    user_c, admin_c, _, _ = two_clients

    user_resp = await user_c.post(
        "/api/v1/topics",
        json={"topic_name": "jobs", "is_public": False},
    )
    admin_resp = await admin_c.post(
        "/api/v1/topics",
        json={"topic_name": "jobs", "is_public": False},
    )
    assert user_resp.status_code == 201, user_resp.text
    assert admin_resp.status_code == 201, admin_resp.text


@pytest.mark.anyio
async def test_publish_isolation_between_namespaces(two_clients) -> None:
    """Messages user A publishes to ``"jobs"`` are invisible when user
    B reads their own ``"jobs"`` — different streams."""
    user_c, admin_c, _, _ = two_clients

    # Each publishes to their own "jobs".
    await user_c.post("/api/v1/messages", json={"topic": "jobs", "payload": {"from": "user"}})
    await admin_c.post("/api/v1/messages", json={"topic": "jobs", "payload": {"from": "admin"}})

    # User reads back only their own message.
    user_msgs = await user_c.get("/api/v1/topics/jobs/messages")
    assert user_msgs.status_code == 200
    bodies = [m["payload"] for m in user_msgs.json()["messages"]]
    assert bodies == [{"from": "user"}]

    # Admin reads back only theirs.
    admin_msgs = await admin_c.get("/api/v1/topics/jobs/messages")
    assert admin_msgs.status_code == 200
    bodies = [m["payload"] for m in admin_msgs.json()["messages"]]
    assert bodies == [{"from": "admin"}]


@pytest.mark.anyio
async def test_scan_for_legacy_keys_returns_examples() -> None:
    """``scan_for_legacy_keys`` flags pre-namespacing keys (no ``/`` in
    the name portion). New-format keys (with a ``/``) are not flagged."""

    class _StubClient:
        """Minimal GLIDE-shaped stub. Each new match pattern is treated
        as a fresh iteration: ``scan_for_legacy_keys`` walks
        ``topic:*``, ``stream:topic:*``, and ``meta:topic:*`` in turn."""

        def __init__(self, keys: list[bytes]) -> None:
            self._keys = keys
            self._yielded_for: set[str] = set()

        async def scan(self, cursor, match=None, count=None):
            prefix = match.rstrip("*") if match else ""
            if prefix not in self._yielded_for:
                self._yielded_for.add(prefix)
                hits = [k for k in self._keys if k.decode("utf-8").startswith(prefix)]
                return [b"0", hits]
            return [b"0", []]

    legacy = await scan_for_legacy_keys(
        _StubClient(
            [
                b"topic:legacy-jobs",  # flat — flagged
                b"topic:alice/new-jobs",  # namespaced — fine
                b"topic:legacy-other:allowed_users",  # flat + dead-feature suffix — flagged
                b"topic:alice/jobs:allowed_users",  # dead-feature suffix — flagged (Phase 4)
                b"stream:topic:legacy-stream",  # flat — flagged
                b"stream:topic:bob/new-stream",  # namespaced — fine
            ]
        ),
        limit=10,
    )
    flagged = set(legacy)
    assert "topic:legacy-jobs" in flagged
    assert "topic:legacy-other:allowed_users" in flagged
    assert "stream:topic:legacy-stream" in flagged
    assert "topic:alice/new-jobs" not in flagged
    # Phase 4 removed the cross-user-sharing feature, so any key
    # bearing the ``:allowed_users`` suffix is now also legacy.
    assert "topic:alice/jobs:allowed_users" in flagged
    assert "stream:topic:bob/new-stream" not in flagged
