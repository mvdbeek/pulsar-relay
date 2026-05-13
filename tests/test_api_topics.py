"""Tests for topic management and messages API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from pulsar_relay.api import messages, topics
from pulsar_relay.auth.dependencies import set_topic_storage, set_user_storage
from pulsar_relay.auth.jwt import create_access_token
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage
from pulsar_relay.main import app
from pulsar_relay.storage.memory import MemoryStorage


@pytest.fixture
async def client(auth_storage):
    """Create test client with fresh storage and authentication."""
    # Set up message storage
    storage = MemoryStorage()
    messages.set_storage(storage)
    topics.set_storage(storage)

    # Set up topic storage
    topic_storage = InMemoryTopicStorage()
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage

    # Set up authentication
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    # Get test user and create token
    test_user = await auth_storage.get_user_by_username("user")
    token = create_access_token(test_user)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as ac:
        yield ac

    await storage.clear()


class TestGetTopicMessages:
    """Tests for GET /api/v1/topics/{topic_name}/messages endpoint."""

    async def test_get_messages_empty_topic(self, client):
        """Test getting messages from an empty topic."""
        # Create a topic first
        response = await client.post(
            "/api/v1/topics",
            json={"topic_name": "empty-topic", "description": "Empty test topic"},
        )
        assert response.status_code == 201

        # Get messages from empty topic
        response = await client.get("/api/v1/topics/empty-topic/messages")
        assert response.status_code == 200

        data = response.json()
        assert data["messages"] == []
        assert data["total"] == 0
        assert data["limit"] == 10
        assert data["order"] == "desc"  # Default order
        assert data["cursor"] is None
        assert data["next_cursor"] is None

    async def test_get_messages_with_data(self, client):
        """Test getting messages from a topic with data (default desc order)."""
        # Create messages first
        for i in range(5):
            response = await client.post(
                "/api/v1/messages",
                json={
                    "topic": "test-topic",
                    "payload": {"id": i, "message": f"Test message {i}"},
                    "metadata": {"index": str(i)},
                },
            )
            assert response.status_code == 201

        # Get messages - should be in reverse order (newest first) by default
        response = await client.get("/api/v1/topics/test-topic/messages")
        assert response.status_code == 200

        data = response.json()
        assert data["total"] == 5
        assert len(data["messages"]) == 5
        assert data["limit"] == 10
        assert data["order"] == "desc"

        # Verify messages are in reverse chronological order (newest first)
        messages = data["messages"]
        for i, msg in enumerate(messages):
            expected_id = 4 - i  # Reversed order
            assert msg["topic"] == "test-topic"
            assert msg["payload"]["id"] == expected_id
            assert msg["payload"]["message"] == f"Test message {expected_id}"
            assert msg["metadata"]["index"] == str(expected_id)
            assert "message_id" in msg
            assert "timestamp" in msg

        # Verify next_cursor is the last message ID
        assert data["next_cursor"] == messages[-1]["message_id"]

    async def test_get_messages_with_limit(self, client):
        """Test pagination with limit parameter (desc order)."""
        # Create 10 messages
        for i in range(10):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "paginated-topic", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get first 3 messages (newest first with desc order)
        response = await client.get("/api/v1/topics/paginated-topic/messages?limit=3")
        assert response.status_code == 200

        data = response.json()
        assert data["total"] == 3
        assert len(data["messages"]) == 3
        assert data["limit"] == 3
        assert data["order"] == "desc"

        # Verify newest messages come first (9, 8, 7)
        assert data["messages"][0]["payload"]["id"] == 9
        assert data["messages"][1]["payload"]["id"] == 8
        assert data["messages"][2]["payload"]["id"] == 7

    async def test_get_messages_with_pagination_asc(self, client):
        """Test pagination with order=asc (forward in time)."""
        # Create 10 messages
        for i in range(10):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "page-asc", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get first page (oldest messages with asc order)
        response = await client.get("/api/v1/topics/page-asc/messages?limit=3&order=asc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 3
        assert data["order"] == "asc"
        # Should get messages 0, 1, 2 (oldest first)
        assert data["messages"][0]["payload"]["id"] == 0
        assert data["messages"][1]["payload"]["id"] == 1
        assert data["messages"][2]["payload"]["id"] == 2

        # Get second page using cursor (should get messages 3, 4, 5)
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/page-asc/messages?limit=3&order=asc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 3
        assert data["cursor"] == cursor
        assert data["messages"][0]["payload"]["id"] == 3
        assert data["messages"][1]["payload"]["id"] == 4
        assert data["messages"][2]["payload"]["id"] == 5

    async def test_get_messages_with_pagination_desc(self, client):
        """Test pagination with order=desc (backward in time)."""
        # Create 10 messages
        for i in range(10):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "page-desc", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get first page (newest messages with desc order)
        response = await client.get("/api/v1/topics/page-desc/messages?limit=3&order=desc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 3
        assert data["order"] == "desc"
        # Should get messages 9, 8, 7 (newest first)
        assert data["messages"][0]["payload"]["id"] == 9
        assert data["messages"][1]["payload"]["id"] == 8
        assert data["messages"][2]["payload"]["id"] == 7

        # Get second page using cursor (should get messages 6, 5, 4)
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/page-desc/messages?limit=3&order=desc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 3
        assert data["cursor"] == cursor
        assert data["messages"][0]["payload"]["id"] == 6
        assert data["messages"][1]["payload"]["id"] == 5
        assert data["messages"][2]["payload"]["id"] == 4

    async def test_get_messages_limit_validation(self, client):
        """Test limit parameter validation."""
        # Create a topic
        await client.post(
            "/api/v1/topics",
            json={"topic_name": "limit-test", "description": "Test topic"},
        )

        # Test limit < 1
        response = await client.get("/api/v1/topics/limit-test/messages?limit=0")
        assert response.status_code == 400

        # Test negative limit
        response = await client.get("/api/v1/topics/limit-test/messages?limit=-1")
        assert response.status_code == 400

        # Test limit > 100 (should be capped to 100)
        for i in range(150):
            await client.post(
                "/api/v1/messages",
                json={"topic": "limit-test", "payload": {"id": i}},
            )

        response = await client.get("/api/v1/topics/limit-test/messages?limit=150")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 100
        assert len(data["messages"]) == 100

    async def test_get_messages_nonexistent_topic(self, client):
        """Test getting messages from a non-existent topic."""
        response = await client.get("/api/v1/topics/nonexistent/messages")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    async def test_get_messages_other_user_topic_returns_404(self, auth_storage, client):
        """Under per-user topic namespacing (API H#5), another user's
        topic is simply invisible — the bearer's namespace doesn't
        contain it. The endpoint returns 404, not 403."""
        other_user = await auth_storage.get_user_by_username("admin")
        other_token = create_access_token(other_user)

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {other_token}"}
        ) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "private-topic", "is_public": False, "description": "Private topic"},
            )
            assert response.status_code == 201

        # Regular user tries to access — looks up (user, private-topic),
        # which doesn't exist. 404, not 403.
        response = await client.get("/api/v1/topics/private-topic/messages")
        assert response.status_code == 404

    async def test_get_messages_with_metadata(self, client):
        """Test that messages with metadata are properly returned."""
        # Create message with metadata
        response = await client.post(
            "/api/v1/messages",
            json={
                "topic": "metadata-topic",
                "payload": {"data": "test"},
                "metadata": {"key1": "value1", "key2": "value2"},
            },
        )
        assert response.status_code == 201

        # Get messages
        response = await client.get("/api/v1/topics/metadata-topic/messages")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 1
        msg = data["messages"][0]
        assert msg["metadata"]["key1"] == "value1"
        assert msg["metadata"]["key2"] == "value2"

    async def test_get_messages_default_limit(self, client):
        """Test that default limit is 10."""
        # Create 15 messages
        for i in range(15):
            await client.post(
                "/api/v1/messages",
                json={"topic": "default-limit", "payload": {"id": i}},
            )

        # Get messages without specifying limit
        response = await client.get("/api/v1/topics/default-limit/messages")
        assert response.status_code == 200

        data = response.json()
        assert data["limit"] == 10
        assert len(data["messages"]) == 10

    async def test_get_messages_invalid_order(self, client):
        """Test that invalid order parameter returns 400."""
        # Create a topic
        await client.post(
            "/api/v1/topics",
            json={"topic_name": "order-test", "description": "Test topic"},
        )

        # Test invalid order
        response = await client.get("/api/v1/topics/order-test/messages?order=invalid")
        assert response.status_code == 400
        assert "order" in response.json()["detail"].lower()


class TestTopicAccessControl:
    """Cross-user access-control tests for the topics API.

    The default `client` fixture is authenticated as the regular `user` account.
    The `admin` account is used as a second user that owns topics the regular
    user should not be able to read.
    """

    async def _admin_client(self, auth_storage):
        """Build an AsyncClient authenticated as the admin user."""
        admin_user = await auth_storage.get_user_by_username("admin")
        admin_token = create_access_token(admin_user)
        transport = ASGITransport(app=app)
        return AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    async def test_get_topic_detail_returns_404_for_other_users_private_topic(self, auth_storage, client):
        """Under per-user namespacing (API H#5), another user's topic
        is invisible: GET on the bare name looks up ``(bearer, name)``,
        which does not exist → 404."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-private", "is_public": False},
            )
            assert response.status_code == 201

        response = await client.get("/api/v1/topics/admins-private")
        assert response.status_code == 404

    async def test_list_topics_excludes_other_users_private_topic(self, auth_storage, client):
        """GET /api/v1/topics for a non-admin must not include another user's private topic."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-hidden", "is_public": False},
            )
            assert response.status_code == 201

        # Regular user creates one of their own to confirm filtering doesn't drop everything
        response = await client.post(
            "/api/v1/topics",
            json={"topic_name": "users-own", "is_public": False},
        )
        assert response.status_code == 201

        response = await client.get("/api/v1/topics")
        assert response.status_code == 200
        names = {t["topic_name"] for t in response.json()}
        assert "users-own" in names
        assert "admins-hidden" not in names

    async def test_list_topics_excludes_other_users_public_topic(self, auth_storage, client):
        """Public topics owned by others are not returned by list_user_topics for non-admins.

        This documents the current behavior: list_user_topics only returns owned +
        explicitly-granted topics. A user can still GET a specific public topic by name.
        """
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-public", "is_public": True},
            )
            assert response.status_code == 201

        response = await client.get("/api/v1/topics")
        assert response.status_code == 200
        names = {t["topic_name"] for t in response.json()}
        assert "admins-public" not in names

    async def test_get_topic_detail_returns_404_for_other_users_public_topic(self, auth_storage, client):
        """Per-user namespacing means even ``is_public=True`` doesn't
        expose another user's topic via the bare-name wire — the
        bearer's namespace doesn't contain it. ``is_public`` retains
        meaning only when a caller can address ``(owner_id, name)``,
        which the current wire does not support."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-public-readable", "is_public": True},
            )
            assert response.status_code == 201

        response = await client.get("/api/v1/topics/admins-public-readable")
        assert response.status_code == 404

    async def test_get_messages_returns_empty_for_other_users_public_topic(self, auth_storage, client):
        """Similarly: messages published to another user's public topic
        are not visible via the bearer's bare-name path."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-public-msgs", "is_public": True},
            )
            assert response.status_code == 201
            response = await admin_client.post(
                "/api/v1/messages",
                json={"topic": "admins-public-msgs", "payload": {"hello": "world"}},
            )
            assert response.status_code == 201

        # Bearer looks up (user, admins-public-msgs) → topic not found.
        response = await client.get("/api/v1/topics/admins-public-msgs/messages")
        assert response.status_code == 404

    async def test_two_users_can_have_same_topic_name(self, auth_storage, client):
        """Core API H#5 invariant: two users each create a topic called
        ``"jobs"`` and both succeed. The squat is gone."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "jobs", "is_public": False},
            )
            assert response.status_code == 201, response.text

        # Regular user creates THEIR "jobs" — no collision.
        response = await client.post(
            "/api/v1/topics",
            json={"topic_name": "jobs", "is_public": False},
        )
        assert response.status_code == 201, response.text

    async def test_publish_to_unseen_topic_name_auto_creates_under_bearer(self, auth_storage, client):
        """When admin owns "owner-only" and a regular user POSTs a
        message to "owner-only", under namespacing the publish lands in
        the bearer's own namespace (user, owner-only) — not admin's.
        The user is auto-created as the new owner; no 403."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "owner-only", "is_public": False},
            )
            assert response.status_code == 201

        response = await client.post(
            "/api/v1/messages",
            json={"topic": "owner-only", "payload": {"from": "user"}},
        )
        assert response.status_code == 201

        # User's "owner-only" topic exists and admin's is untouched.
        user_msgs = await client.get("/api/v1/topics/owner-only/messages")
        assert user_msgs.status_code == 200
        assert len(user_msgs.json()["messages"]) == 1

    async def test_publish_blocked_for_unrelated_dummy_topic(self, auth_storage, client):
        """Sanity check: a publish path that did not previously
        auto-create the topic (because of a write-permission check
        below the auto-create) still produces a sensible status. With
        bearer-scoped namespacing the bearer always gets write access
        to their own topic, so this is mostly redundant — kept as a
        smoke test."""
        response = await client.post(
            "/api/v1/messages",
            json={"topic": "smoke-test", "payload": {"x": 1}},
        )
        assert response.status_code == 201

    async def test_bulk_publish_succeeds_for_bearers_own_namespace(self, auth_storage, client):
        """Bulk publish to a topic name another user owns now lands in
        the bearer's namespace (auto-created). No 403 because there is
        no collision."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "bulk-shared-name", "is_public": False},
            )
            assert response.status_code == 201

        response = await client.post(
            "/api/v1/messages/bulk",
            json={"messages": [{"topic": "bulk-shared-name", "payload": {"x": 1}}]},
        )
        # 207 because bulk endpoint returns multi-status. All accepted.
        assert response.status_code in (200, 207), response.text
        data = response.json()
        assert data["summary"]["accepted"] == 1

    async def test_owner_can_publish_to_own_topic(self, auth_storage, client):
        """Sanity: owner can still publish."""
        response = await client.post(
            "/api/v1/topics",
            json={"topic_name": "users-own-topic", "is_public": False},
        )
        assert response.status_code == 201

        response = await client.post(
            "/api/v1/messages",
            json={"topic": "users-own-topic", "payload": {"hello": "self"}},
        )
        assert response.status_code == 201

    async def test_admin_publish_lands_in_admins_own_namespace(self, auth_storage, client):
        """A consequence of per-user namespacing: admin publishing to
        "users-private" creates (admin, users-private), not (user,
        users-private). The user's topic is unaffected. Documents the
        new isolation rather than the old "admin write bypass"
        behaviour."""
        response = await client.post(
            "/api/v1/topics",
            json={"topic_name": "users-private", "is_public": False},
        )
        assert response.status_code == 201

        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/messages",
                json={"topic": "users-private", "payload": {"from": "admin"}},
            )
            assert response.status_code == 201

        # User's own "users-private" did NOT receive admin's message.
        user_msgs = await client.get("/api/v1/topics/users-private/messages")
        assert user_msgs.status_code == 200
        assert user_msgs.json()["messages"] == []

    async def test_publish_to_new_topic_auto_creates_with_caller_as_owner(self, auth_storage, client):
        """Publishing to a non-existent topic auto-creates it with the caller as owner."""
        response = await client.post(
            "/api/v1/messages",
            json={"topic": "auto-created", "payload": {"x": 1}},
        )
        assert response.status_code == 201

        # Caller is now the owner and can read
        response = await client.get("/api/v1/topics/auto-created")
        assert response.status_code == 200
        body = response.json()
        user = await auth_storage.get_user_by_username("user")
        assert body["owner_id"] == user.user_id
