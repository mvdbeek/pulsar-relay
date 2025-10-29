"""Tests for topic management and messages API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import messages, topics
from app.auth.dependencies import set_topic_storage, set_user_storage
from app.auth.jwt import create_access_token
from app.auth.topic_storage import InMemoryTopicStorage
from app.main import app
from app.storage.memory import MemoryStorage


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
