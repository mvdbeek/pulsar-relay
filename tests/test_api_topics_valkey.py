"""Integration tests for topics messages API with Valkey backend.

These tests require a running Valkey instance on localhost:6379.
Start Valkey with: docker run -d -p 6379:6379 valkey/valkey:latest

Run these tests with: VALKEY_INTEGRATION_TEST=1 pytest tests/test_api_topics_valkey.py -v
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import messages, topics
from app.auth.dependencies import set_topic_storage, set_user_storage
from app.auth.jwt import create_access_token
from app.auth.topic_storage import InMemoryTopicStorage
from app.main import app
from app.storage.valkey import ValkeyStorage

pytestmark = pytest.mark.skipif(
    not os.getenv("VALKEY_INTEGRATION_TEST"), reason="VALKEY_INTEGRATION_TEST environment variable not set"
)


@pytest.fixture
async def valkey_storage():
    """Create a ValkeyStorage instance and connect to real Valkey."""
    storage = ValkeyStorage(
        host="localhost",
        port=6379,
        max_messages_per_topic=1000,
        ttl_seconds=3600,
    )

    try:
        await storage.connect()
        # Clear any existing test data
        await storage.clear()
        yield storage
    finally:
        # Cleanup after tests
        try:
            await storage.clear()
        except Exception:
            pass
        await storage.disconnect()


@pytest.fixture
async def client(auth_storage, valkey_storage):
    """Create test client with Valkey storage and authentication."""
    # Set up message storage with Valkey
    messages.set_storage(valkey_storage)
    topics.set_storage(valkey_storage)

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


class TestValkeyGetTopicMessages:
    """Tests for GET /api/v1/topics/{topic_name}/messages with Valkey backend."""

    async def test_get_messages_empty_topic_valkey(self, client):
        """Test getting messages from an empty topic using Valkey."""
        # Create a topic first
        response = await client.post(
            "/api/v1/topics",
            json={"topic_name": "valkey-empty", "description": "Empty test topic"},
        )
        assert response.status_code == 201

        # Get messages from empty topic
        response = await client.get("/api/v1/topics/valkey-empty/messages")
        assert response.status_code == 200

        data = response.json()
        assert data["messages"] == []
        assert data["total"] == 0
        assert data["order"] == "desc"

    async def test_get_messages_with_valkey_stream_ids(self, client):
        """Test that Valkey stream IDs work correctly as cursors."""
        # Create 10 messages
        for i in range(10):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "valkey-stream", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get first page with desc order (newest first)
        response = await client.get("/api/v1/topics/valkey-stream/messages?limit=3&order=desc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 3
        assert data["order"] == "desc"

        # Verify newest messages come first (9, 8, 7)
        assert data["messages"][0]["payload"]["id"] == 9
        assert data["messages"][1]["payload"]["id"] == 8
        assert data["messages"][2]["payload"]["id"] == 7

        # Verify message IDs are Valkey stream IDs (format: "timestamp-sequence")
        for msg in data["messages"]:
            msg_id = msg["message_id"]
            assert "-" in msg_id  # Valkey stream IDs have format "timestamp-sequence"
            parts = msg_id.split("-")
            assert len(parts) == 2
            assert parts[0].isdigit()  # timestamp
            assert parts[1].isdigit()  # sequence

        # Get second page using cursor from Valkey
        cursor = data["next_cursor"]
        assert cursor is not None
        assert "-" in cursor  # Should be a Valkey stream ID

        response = await client.get(f"/api/v1/topics/valkey-stream/messages?limit=3&order=desc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 3
        # Should get messages 6, 5, 4 (continuing backward in time)
        assert data["messages"][0]["payload"]["id"] == 6
        assert data["messages"][1]["payload"]["id"] == 5
        assert data["messages"][2]["payload"]["id"] == 4

    async def test_valkey_pagination_asc_order(self, client):
        """Test forward pagination with Valkey using asc order."""
        # Create 15 messages
        for i in range(15):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "valkey-asc", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get first page with asc order (oldest first)
        response = await client.get("/api/v1/topics/valkey-asc/messages?limit=5&order=asc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5
        assert data["order"] == "asc"

        # Verify oldest messages come first (0, 1, 2, 3, 4)
        for i in range(5):
            assert data["messages"][i]["payload"]["id"] == i

        # Get second page
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/valkey-asc/messages?limit=5&order=asc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5
        # Should get messages 5, 6, 7, 8, 9
        for i in range(5):
            assert data["messages"][i]["payload"]["id"] == i + 5

        # Get third page
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/valkey-asc/messages?limit=5&order=asc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5
        # Should get messages 10, 11, 12, 13, 14
        for i in range(5):
            assert data["messages"][i]["payload"]["id"] == i + 10

    async def test_valkey_pagination_desc_order(self, client):
        """Test backward pagination with Valkey using desc order."""
        # Create 15 messages
        for i in range(15):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "valkey-desc", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get first page with desc order (newest first)
        response = await client.get("/api/v1/topics/valkey-desc/messages?limit=5&order=desc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5
        assert data["order"] == "desc"

        # Verify newest messages come first (14, 13, 12, 11, 10)
        for i in range(5):
            assert data["messages"][i]["payload"]["id"] == 14 - i

        # Get second page
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/valkey-desc/messages?limit=5&order=desc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5
        # Should get messages 9, 8, 7, 6, 5
        for i in range(5):
            assert data["messages"][i]["payload"]["id"] == 9 - i

        # Get third page
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/valkey-desc/messages?limit=5&order=desc&cursor={cursor}")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5
        # Should get messages 4, 3, 2, 1, 0
        for i in range(5):
            assert data["messages"][i]["payload"]["id"] == 4 - i

    async def test_valkey_large_dataset_performance(self, client):
        """Test pagination with a larger dataset to verify Valkey performance."""
        # Create 100 messages
        for i in range(100):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "valkey-large", "payload": {"id": i, "data": f"message_{i}"}},
            )
            assert response.status_code == 201

        # Test getting most recent messages
        response = await client.get("/api/v1/topics/valkey-large/messages?limit=20&order=desc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 20
        # Should get messages 99 down to 80
        assert data["messages"][0]["payload"]["id"] == 99
        assert data["messages"][19]["payload"]["id"] == 80

        # Test pagination through entire dataset
        all_messages = []
        cursor = None
        page_count = 0

        while page_count < 10:  # Get first 10 pages (10 messages each = 100 total)
            url = "/api/v1/topics/valkey-large/messages?limit=10&order=asc"
            if cursor:
                url += f"&cursor={cursor}"

            response = await client.get(url)
            assert response.status_code == 200

            data = response.json()
            all_messages.extend(data["messages"])

            if not data["next_cursor"] or len(data["messages"]) < 10:
                break

            cursor = data["next_cursor"]
            page_count += 1

        # Verify we got all 100 messages in correct order
        assert len(all_messages) == 100
        for i, msg in enumerate(all_messages):
            assert msg["payload"]["id"] == i

    async def test_valkey_metadata_preservation(self, client):
        """Test that metadata is properly stored and retrieved from Valkey."""
        # Create messages with rich metadata
        for i in range(5):
            response = await client.post(
                "/api/v1/messages",
                json={
                    "topic": "valkey-metadata",
                    "payload": {"id": i, "data": f"test_{i}"},
                    "metadata": {
                        "source": "test_suite",
                        "index": str(i),
                        "timestamp": f"2025-01-{i+1:02d}",
                    },
                },
            )
            assert response.status_code == 201

        # Retrieve and verify metadata
        response = await client.get("/api/v1/topics/valkey-metadata/messages?limit=10&order=asc")
        assert response.status_code == 200

        data = response.json()
        assert len(data["messages"]) == 5

        for i, msg in enumerate(data["messages"]):
            assert msg["payload"]["id"] == i
            assert msg["metadata"]["source"] == "test_suite"
            assert msg["metadata"]["index"] == str(i)
            assert msg["metadata"]["timestamp"] == f"2025-01-{i+1:02d}"

    async def test_valkey_mixed_order_requests(self, client):
        """Test switching between asc and desc order with Valkey."""
        # Create 10 messages
        for i in range(10):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "valkey-mixed", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get newest 3 messages (desc)
        response = await client.get("/api/v1/topics/valkey-mixed/messages?limit=3&order=desc")
        assert response.status_code == 200
        data = response.json()
        assert data["messages"][0]["payload"]["id"] == 9
        assert data["messages"][1]["payload"]["id"] == 8
        assert data["messages"][2]["payload"]["id"] == 7

        # Get oldest 3 messages (asc)
        response = await client.get("/api/v1/topics/valkey-mixed/messages?limit=3&order=asc")
        assert response.status_code == 200
        data = response.json()
        assert data["messages"][0]["payload"]["id"] == 0
        assert data["messages"][1]["payload"]["id"] == 1
        assert data["messages"][2]["payload"]["id"] == 2

        # Get middle messages using cursor (asc from message 2)
        cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/valkey-mixed/messages?limit=3&order=asc&cursor={cursor}")
        assert response.status_code == 200
        data = response.json()
        assert data["messages"][0]["payload"]["id"] == 3
        assert data["messages"][1]["payload"]["id"] == 4
        assert data["messages"][2]["payload"]["id"] == 5

    async def test_valkey_cursor_boundary_conditions(self, client):
        """Test edge cases with cursors in Valkey."""
        # Create 5 messages
        for i in range(5):
            response = await client.post(
                "/api/v1/messages",
                json={"topic": "valkey-boundary", "payload": {"id": i}},
            )
            assert response.status_code == 201

        # Get all messages
        response = await client.get("/api/v1/topics/valkey-boundary/messages?limit=100&order=asc")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 5

        # Try to paginate past the end
        last_cursor = data["next_cursor"]
        response = await client.get(f"/api/v1/topics/valkey-boundary/messages?limit=10&order=asc&cursor={last_cursor}")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 0  # No more messages

        # Get messages in desc order
        response = await client.get("/api/v1/topics/valkey-boundary/messages?limit=2&order=desc")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["payload"]["id"] == 4
        assert data["messages"][1]["payload"]["id"] == 3
