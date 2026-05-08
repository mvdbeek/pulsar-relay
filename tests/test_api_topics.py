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

    async def test_get_messages_no_access(self, auth_storage, client):
        """Test access denied when user doesn't have read access."""
        # Create another user and their token
        other_user = await auth_storage.get_user_by_username("admin")
        other_token = create_access_token(other_user)

        # Admin creates a private topic
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {other_token}"}
        ) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "private-topic", "is_public": False, "description": "Private topic"},
            )
            assert response.status_code == 201

        # Regular user tries to access - should be denied
        response = await client.get("/api/v1/topics/private-topic/messages")
        assert response.status_code == 403
        assert "access denied" in response.json()["detail"].lower()

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

    async def test_get_topic_detail_denied_for_other_users_private_topic(self, auth_storage, client):
        """A regular user gets 403 fetching another user's private topic detail."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-private", "is_public": False},
            )
            assert response.status_code == 201

        response = await client.get("/api/v1/topics/admins-private")
        assert response.status_code == 403
        assert "access denied" in response.json()["detail"].lower()

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

    async def test_get_topic_detail_allowed_for_other_users_public_topic(self, auth_storage, client):
        """Any authenticated user can read a public topic owned by someone else."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "admins-public-readable", "is_public": True},
            )
            assert response.status_code == 201

        response = await client.get("/api/v1/topics/admins-public-readable")
        assert response.status_code == 200
        body = response.json()
        assert body["topic_name"] == "admins-public-readable"
        # Non-owner must not see the allow-list
        assert body["allowed_user_ids"] is None

    async def test_get_messages_allowed_for_other_users_public_topic(self, auth_storage, client):
        """A non-owner can read messages from a public topic."""
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

        response = await client.get("/api/v1/topics/admins-public-msgs/messages")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["payload"] == {"hello": "world"}

    async def test_granted_user_can_read_private_topic(self, auth_storage, client):
        """A user explicitly granted access can read another user's private topic."""
        # Resolve the regular user's id
        user = await auth_storage.get_user_by_username("user")

        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "shared-private", "is_public": False},
            )
            assert response.status_code == 201

            # Regular user is denied before grant
            denied = await client.get("/api/v1/topics/shared-private")
            assert denied.status_code == 403

            # Owner grants access
            grant = await admin_client.post(
                "/api/v1/topics/shared-private/permissions",
                json={"user_id": user.user_id},
            )
            assert grant.status_code == 201

            # Owner publishes a message
            response = await admin_client.post(
                "/api/v1/messages",
                json={"topic": "shared-private", "payload": {"secret": True}},
            )
            assert response.status_code == 201

        # After grant, the regular user can access detail and messages
        response = await client.get("/api/v1/topics/shared-private")
        assert response.status_code == 200
        # Granted (non-owner) caller must not see the allow-list
        assert response.json()["allowed_user_ids"] is None

        response = await client.get("/api/v1/topics/shared-private/messages")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["payload"] == {"secret": True}

        # And it now appears in their topic listing
        response = await client.get("/api/v1/topics")
        assert response.status_code == 200
        names = {t["topic_name"] for t in response.json()}
        assert "shared-private" in names

    async def test_publish_denied_for_other_users_private_topic(self, auth_storage, client):
        """A regular user must NOT be able to publish to another user's private topic."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "owner-only", "is_public": False},
            )
            assert response.status_code == 201

        response = await client.post(
            "/api/v1/messages",
            json={"topic": "owner-only", "payload": {"injected": True}},
        )
        assert response.status_code == 403
        assert "owner-only" in response.json()["detail"]

    async def test_bulk_publish_denied_for_other_users_private_topic(self, auth_storage, client):
        """Bulk publish must also reject when any topic is owned by another user."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "bulk-owner-only", "is_public": False},
            )
            assert response.status_code == 201

        response = await client.post(
            "/api/v1/messages/bulk",
            json={"messages": [{"topic": "bulk-owner-only", "payload": {"x": 1}}]},
        )
        assert response.status_code == 403
        assert "bulk-owner-only" in response.json()["detail"]

    async def test_publish_denied_for_granted_user(self, auth_storage, client):
        """Grants are read-only; granted users cannot publish."""
        user = await auth_storage.get_user_by_username("user")

        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "shared-readonly", "is_public": False},
            )
            assert response.status_code == 201

            grant = await admin_client.post(
                "/api/v1/topics/shared-readonly/permissions",
                json={"user_id": user.user_id},
            )
            assert grant.status_code == 201

        # Granted user can read
        response = await client.get("/api/v1/topics/shared-readonly")
        assert response.status_code == 200

        # But cannot publish
        response = await client.post(
            "/api/v1/messages",
            json={"topic": "shared-readonly", "payload": {"x": 1}},
        )
        assert response.status_code == 403

    async def test_publish_denied_for_public_topic_non_owner(self, auth_storage, client):
        """Public topics allow read but not write for non-owners."""
        async with await self._admin_client(auth_storage) as admin_client:
            response = await admin_client.post(
                "/api/v1/topics",
                json={"topic_name": "public-read-only", "is_public": True},
            )
            assert response.status_code == 201

        # Non-owner can read
        response = await client.get("/api/v1/topics/public-read-only")
        assert response.status_code == 200

        # But cannot publish
        response = await client.post(
            "/api/v1/messages",
            json={"topic": "public-read-only", "payload": {"x": 1}},
        )
        assert response.status_code == 403

    async def test_owner_can_publish_to_own_topic(self, auth_storage, client):
        """Sanity: owner can still publish."""
        # Regular user creates and publishes to their own topic
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

    async def test_admin_can_publish_to_any_topic(self, auth_storage, client):
        """Sanity: admin write bypass still works."""
        # Regular user creates a private topic
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
