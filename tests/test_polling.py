"""Tests for long polling functionality."""

import asyncio
import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.polling import PollManager, PollWaiter
from app.main import app
from app.storage.memory import MemoryStorage


@pytest.fixture
def poll_manager():
    """Create a fresh PollManager for testing."""
    return PollManager()


@pytest.fixture
async def test_storage():
    """Create a test storage backend."""
    storage = MemoryStorage()
    return storage


class TestPollWaiter:
    """Test PollWaiter class."""

    @pytest.mark.asyncio
    async def test_waiter_creation(self):
        """Test creating a poll waiter."""
        waiter = PollWaiter("client_123", ["topic1", "topic2"])

        assert waiter.client_id == "client_123"
        assert waiter.topics == {"topic1", "topic2"}
        assert waiter.queue.empty()

    @pytest.mark.asyncio
    async def test_put_and_wait_for_messages(self):
        """Test putting messages and waiting for them."""
        waiter = PollWaiter("client_123", ["topic1"])

        # Put a message
        message = {"topic": "topic1", "payload": {"data": "test"}}
        await waiter.put_message(message)

        # Wait should return immediately with the message
        messages = await waiter.wait_for_messages(timeout=1.0)
        assert len(messages) == 1
        assert messages[0]["payload"]["data"] == "test"

    @pytest.mark.asyncio
    async def test_wait_timeout(self):
        """Test waiting times out when no messages arrive."""
        waiter = PollWaiter("client_123", ["topic1"])

        # Wait should timeout and return empty list
        messages = await waiter.wait_for_messages(timeout=0.1)
        assert messages == []

    @pytest.mark.asyncio
    async def test_multiple_messages(self):
        """Test collecting multiple messages."""
        waiter = PollWaiter("client_123", ["topic1"])

        # Put multiple messages
        for i in range(5):
            await waiter.put_message({"index": i})

        # Wait should collect all messages
        messages = await waiter.wait_for_messages(timeout=0.1)
        assert len(messages) == 5
        assert [m["index"] for m in messages] == [0, 1, 2, 3, 4]


class TestPollManager:
    """Test PollManager class."""

    @pytest.mark.asyncio
    async def test_create_waiter(self, poll_manager):
        """Test creating a waiter through the manager."""
        waiter = await poll_manager.create_waiter(["topic1", "topic2"])

        assert waiter.client_id in poll_manager._waiters
        assert "topic1" in poll_manager._topic_subscribers
        assert "topic2" in poll_manager._topic_subscribers
        assert waiter.client_id in poll_manager._topic_subscribers["topic1"]

    @pytest.mark.asyncio
    async def test_remove_waiter(self, poll_manager):
        """Test removing a waiter."""
        waiter = await poll_manager.create_waiter(["topic1"])
        client_id = waiter.client_id

        # Verify waiter exists
        assert client_id in poll_manager._waiters

        # Remove waiter
        await poll_manager.remove_waiter(client_id)

        # Verify waiter is gone
        assert client_id not in poll_manager._waiters
        assert client_id not in poll_manager._topic_subscribers.get("topic1", set())

    @pytest.mark.asyncio
    async def test_broadcast_to_topic(self, poll_manager):
        """Test broadcasting messages to topic subscribers."""
        # Create multiple waiters
        waiter1 = await poll_manager.create_waiter(["topic1"])
        waiter2 = await poll_manager.create_waiter(["topic1", "topic2"])
        waiter3 = await poll_manager.create_waiter(["topic2"])

        # Broadcast to topic1
        message = {"topic": "topic1", "data": "test"}
        count = await poll_manager.broadcast_to_topic("topic1", message)

        # Should reach waiter1 and waiter2
        assert count == 2

        # Verify messages were queued
        msg1 = await asyncio.wait_for(waiter1.queue.get(), timeout=0.1)
        msg2 = await asyncio.wait_for(waiter2.queue.get(), timeout=0.1)
        assert msg1["data"] == "test"
        assert msg2["data"] == "test"

        # waiter3 should not have received it
        assert waiter3.queue.empty()

    @pytest.mark.asyncio
    async def test_get_stats(self, poll_manager):
        """Test getting manager statistics."""
        await poll_manager.create_waiter(["topic1"])
        await poll_manager.create_waiter(["topic1", "topic2"])

        stats = poll_manager.get_stats()

        assert stats["active_waiters"] == 2
        assert stats["subscribed_topics"] == 2
        assert stats["topic_subscriber_counts"]["topic1"] == 2
        assert stats["topic_subscriber_counts"]["topic2"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_stale_waiters(self, poll_manager):
        """Test cleaning up stale waiters."""
        waiter = await poll_manager.create_waiter(["topic1"])

        # Manually set created_at to past
        waiter.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=400)

        # Cleanup with 300 second max age
        removed = await poll_manager.cleanup_stale_waiters(max_age_seconds=300)

        assert removed == 1
        assert waiter.client_id not in poll_manager._waiters


# Use auth_storage fixture from conftest.py


@pytest.fixture
def auth_token(auth_storage):
    """Create an auth token for a test user."""
    import asyncio

    from app.auth.jwt import create_access_token

    loop = asyncio.get_event_loop()
    user = loop.run_until_complete(auth_storage.get_user_by_username("user"))
    return create_access_token(user)


@pytest.fixture
def test_client(test_storage, poll_manager, auth_storage):
    """Create a test client with properly initialized app state."""
    from app.auth.dependencies import set_topic_storage, set_user_storage
    from app.auth.topic_storage import InMemoryTopicStorage

    topic_storage = InMemoryTopicStorage()

    set_user_storage(auth_storage)
    set_topic_storage(topic_storage)
    app.state.storage = test_storage
    app.state.poll_manager = poll_manager
    app.state.user_storage = auth_storage
    app.state.topic_storage = topic_storage
    return TestClient(app)


class TestPollingEndpoint:
    """Test the long polling HTTP endpoint."""

    @pytest.mark.asyncio
    async def test_poll_returns_200(self, test_client, auth_token):
        """Test polling endpoint returns successfully."""
        # Basic test that endpoint works and returns valid response
        response = test_client.post(
            "/messages/poll",
            json={
                "topics": ["test-topic"],
                "timeout": 1,  # Short timeout
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "messages" in data
        assert "has_more" in data
        assert isinstance(data["messages"], list)

    @pytest.mark.asyncio
    async def test_poll_timeout(self, test_client, auth_token):
        """Test polling times out when no messages arrive."""
        # Poll with short timeout
        response = test_client.post(
            "/messages/poll",
            json={
                "topics": ["non-existent-topic"],
                "since": None,
                "timeout": 1,  # 1 second timeout
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["messages"] == []
        assert data["has_more"] is False

    @pytest.mark.asyncio
    async def test_poll_receives_new_message(self, real_server):
        """Test polling receives a message that arrives during wait.

        This test uses a real server instance to properly test concurrent
        polling and message publishing.
        """
        base_url = real_server["base_url"]
        username = real_server["username"]
        password = real_server["password"]

        async with httpx.AsyncClient() as client:
            # Login to get a token
            login_response = await client.post(
                f"{base_url}/auth/login",
                data={"username": username, "password": password},
            )
            assert login_response.status_code == 200
            token = login_response.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            # Start a long poll in the background (10 second timeout)
            async def start_poll():
                poll_response = await client.post(
                    f"{base_url}/messages/poll",
                    json={
                        "topics": ["test-topic"],
                        "timeout": 10,
                    },
                    headers=headers,
                    timeout=15.0,  # HTTP timeout longer than poll timeout
                )
                return poll_response

            # Send a message after a short delay
            async def send_message():
                # Wait a bit to ensure poll request is waiting
                await asyncio.sleep(1)

                # Send a message to the topic
                msg_response = await client.post(
                    f"{base_url}/api/v1/messages",
                    json={
                        "topic": "test-topic",
                        "payload": {"data": "test message"},
                    },
                    headers=headers,
                )
                return msg_response

            # Run both operations concurrently
            poll_task = asyncio.create_task(start_poll())
            msg_task = asyncio.create_task(send_message())

            # Wait for both to complete
            poll_response, msg_response = await asyncio.gather(poll_task, msg_task)

            # Verify message was sent successfully
            assert msg_response.status_code == 201
            message_id = msg_response.json()["message_id"]

            # Verify poll received the message
            assert poll_response.status_code == 200
            poll_data = poll_response.json()
            assert "messages" in poll_data
            assert len(poll_data["messages"]) == 1
            assert poll_data["messages"][0]["message_id"] == message_id
            assert poll_data["messages"][0]["topic"] == "test-topic"
            assert poll_data["messages"][0]["payload"]["data"] == "test message"

    @pytest.mark.asyncio
    async def test_poll_with_since_parameter(self, test_storage, test_client, auth_token):
        """Test polling with since parameter for pagination."""

        # Save multiple messages
        await test_storage.save_message(
            "msg_1", "test-topic", {"index": 1}, datetime.datetime.now(datetime.timezone.utc)
        )
        await test_storage.save_message(
            "msg_2", "test-topic", {"index": 2}, datetime.datetime.now(datetime.timezone.utc)
        )
        await test_storage.save_message(
            "msg_3", "test-topic", {"index": 3}, datetime.datetime.now(datetime.timezone.utc)
        )

        # Poll with since=msg_1 (should get msg_2 and msg_3)
        response = test_client.post(
            "/messages/poll",
            json={
                "topics": ["test-topic"],
                "since": {"test-topic": "msg_1"},
                "timeout": 1,
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        # Should get messages after msg_1
        assert len(data["messages"]) >= 2

    @pytest.mark.asyncio
    async def test_poll_invalid_request(self, test_client, auth_token):
        """Test polling with invalid request."""
        # Empty topics list
        response = test_client.post(
            "/messages/poll",
            json={
                "topics": [],
                "timeout": 30,
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_poll_stats_endpoint(self, test_client, auth_token):
        """Test the poll stats endpoint."""
        response = test_client.get("/messages/poll/stats", headers={"Authorization": f"Bearer {auth_token}"})

        assert response.status_code == 200
        data = response.json()
        assert "active_waiters" in data
        assert "subscribed_topics" in data

    @pytest.mark.asyncio
    async def test_poll_multiple_topics(self, test_client, auth_token):
        """Test polling multiple topics simultaneously."""
        response = test_client.post(
            "/messages/poll",
            json={
                "topics": ["topic1", "topic2", "topic3"],
                "since": None,
                "timeout": 1,
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["messages"], list)
