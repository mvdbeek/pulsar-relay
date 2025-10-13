"""Tests for message ingestion API."""

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.storage.memory import MemoryStorage
from app.api import messages, health
from app.auth.storage import InMemoryUserStorage, create_default_users
from app.auth.dependencies import set_user_storage
from app.auth.jwt import create_access_token


@pytest.fixture
async def client():
    """Create test client with fresh storage and authentication."""
    # Set up message storage
    storage = MemoryStorage()
    messages.set_storage(storage)
    health.set_storage(storage)

    # Set up authentication
    user_storage = InMemoryUserStorage()
    await create_default_users(user_storage)
    set_user_storage(user_storage)
    app.state.user_storage = user_storage

    # Get test user and create token
    test_user = await user_storage.get_user_by_username("user")
    token = create_access_token(test_user)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"}
    ) as ac:
        yield ac

    await storage.clear()


@pytest.mark.asyncio
class TestCreateMessage:
    """Tests for POST /api/v1/messages endpoint."""

    async def test_create_message_success(self, client):
        """Test successfully creating a message."""
        response = await client.post(
            "/api/v1/messages",
            json={
                "topic": "notifications",
                "payload": {"user_id": 123, "message": "Hello"},
                "ttl": 3600,
                "metadata": {"priority": "high"},
            },
        )

        assert response.status_code == 201
        data = response.json()

        assert "message_id" in data
        assert data["message_id"].startswith("msg_")
        assert data["topic"] == "notifications"
        assert "timestamp" in data

    async def test_create_message_minimal(self, client):
        """Test creating a message with minimal fields."""
        response = await client.post(
            "/api/v1/messages", json={"topic": "test", "payload": {"data": "value"}}
        )

        assert response.status_code == 201
        data = response.json()

        assert "message_id" in data
        assert data["topic"] == "test"

    async def test_create_message_invalid_topic(self, client):
        """Test creating a message with invalid topic."""
        response = await client.post(
            "/api/v1/messages", json={"topic": "invalid@topic!", "payload": {"data": "value"}}
        )

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    async def test_create_message_empty_topic(self, client):
        """Test creating a message with empty topic."""
        response = await client.post(
            "/api/v1/messages", json={"topic": "", "payload": {"data": "value"}}
        )

        assert response.status_code == 422

    async def test_create_message_missing_payload(self, client):
        """Test creating a message without payload."""
        response = await client.post("/api/v1/messages", json={"topic": "test"})

        assert response.status_code == 422

    async def test_create_message_invalid_ttl(self, client):
        """Test creating a message with invalid TTL."""
        response = await client.post(
            "/api/v1/messages", json={"topic": "test", "payload": {"data": "value"}, "ttl": -1}
        )

        assert response.status_code == 422

    async def test_message_persisted_to_storage(self, client):
        """Test that created message is persisted to storage."""
        # Create message
        response = await client.post(
            "/api/v1/messages", json={"topic": "test-persist", "payload": {"data": "test"}}
        )

        assert response.status_code == 201
        message_id = response.json()["message_id"]

        # Verify it's in storage
        storage = messages.get_storage()
        stored_messages = await storage.get_messages("test-persist")

        assert len(stored_messages) == 1
        assert stored_messages[0]["message_id"] == message_id
        assert stored_messages[0]["payload"] == {"data": "test"}


@pytest.mark.asyncio
class TestCreateBulkMessages:
    """Tests for POST /api/v1/messages/bulk endpoint."""

    async def test_create_bulk_messages_success(self, client):
        """Test successfully creating multiple messages."""
        response = await client.post(
            "/api/v1/messages/bulk",
            json={
                "messages": [
                    {"topic": "topic1", "payload": {"id": 1}},
                    {"topic": "topic2", "payload": {"id": 2}},
                    {"topic": "topic3", "payload": {"id": 3}},
                ]
            },
        )

        assert response.status_code == 207
        data = response.json()

        assert "results" in data
        assert "summary" in data
        assert len(data["results"]) == 3

        # Check summary
        assert data["summary"]["total"] == 3
        assert data["summary"]["accepted"] == 3
        assert data["summary"]["rejected"] == 0

        # Check results
        for i, result in enumerate(data["results"]):
            assert result["status"] == "accepted"
            assert result["message_id"].startswith("msg_")
            assert result["topic"] == f"topic{i+1}"
            assert result["error"] is None

    async def test_create_bulk_messages_partial_failure(self, client):
        """Test bulk creation with some invalid messages."""
        response = await client.post(
            "/api/v1/messages/bulk",
            json={
                "messages": [
                    {"topic": "topic1", "payload": {"id": 1}},
                    {"topic": "invalid@topic", "payload": {"id": 2}},  # Invalid
                    {"topic": "topic3", "payload": {"id": 3}},
                ]
            },
        )

        # Should still return 207 even with validation errors in request
        # But validation happens before this endpoint, so it will be 422
        assert response.status_code == 422

    async def test_create_bulk_messages_empty(self, client):
        """Test bulk creation with empty messages list."""
        response = await client.post("/api/v1/messages/bulk", json={"messages": []})

        assert response.status_code == 422

    async def test_create_bulk_messages_too_many(self, client):
        """Test bulk creation with too many messages."""
        messages_list = [{"topic": f"topic{i}", "payload": {"id": i}} for i in range(101)]

        response = await client.post("/api/v1/messages/bulk", json={"messages": messages_list})

        assert response.status_code == 422

    async def test_bulk_messages_persisted_to_storage(self, client):
        """Test that bulk messages are persisted to storage."""
        response = await client.post(
            "/api/v1/messages/bulk",
            json={
                "messages": [
                    {"topic": "bulk-test", "payload": {"id": 1}},
                    {"topic": "bulk-test", "payload": {"id": 2}},
                ]
            },
        )

        assert response.status_code == 207

        # Verify they're in storage
        storage = messages.get_storage()
        stored_messages = await storage.get_messages("bulk-test", limit=100)

        assert len(stored_messages) == 2
        assert stored_messages[0]["payload"] == {"id": 1}
        assert stored_messages[1]["payload"] == {"id": 2}


@pytest.mark.asyncio
class TestHealthEndpoints:
    """Tests for health check endpoints."""

    async def test_health_check(self, client):
        """Test health check endpoint."""
        response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "healthy"
        assert "timestamp" in data
        assert data["version"] == "0.1.0"

    async def test_readiness_check(self, client):
        """Test readiness check endpoint."""
        response = await client.get("/ready")

        assert response.status_code == 200
        data = response.json()

        assert "ready" in data
        assert "checks" in data
        assert "storage" in data["checks"]
        assert data["checks"]["storage"] == "ok"
        assert data["ready"] is True
