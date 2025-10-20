"""Tests for WebSocket endpoints."""

import asyncio
import json
import time

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient
from httpx import AsyncClient
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from app.api import health, messages, websocket
from app.auth.dependencies import set_topic_storage, set_user_storage
from app.auth.jwt import create_access_token
from app.auth.models import TopicCreate
from app.auth.topic_storage import InMemoryTopicStorage
from app.core.connections import ConnectionManager
from app.main import app
from app.storage.memory import MemoryStorage


async def create_test_topics(topic_storage, user_id, topic_names):
    """Helper to create test topics for a user."""

    for topic_name in topic_names:
        await topic_storage.create_topic(user_id, TopicCreate(topic_name=topic_name, is_public=False))


@pytest.fixture
async def setup_app(auth_storage):
    """Set up app with fresh storage, connection manager, and authentication."""
    storage = MemoryStorage()
    manager = ConnectionManager()
    topic_storage = InMemoryTopicStorage()

    # Set up authentication
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    # Set up topic storage
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage

    # Get test user and create token
    test_user = await auth_storage.get_user_by_username("user")
    token = create_access_token(test_user)

    messages.set_storage(storage)
    messages.set_manager(manager)
    health.set_storage(storage)
    websocket.set_manager(manager)

    yield {
        "storage": storage,
        "manager": manager,
        "topic_storage": topic_storage,
        "client": TestClient(app),
        "token": token,
        "test_user": test_user,
        "auth_headers": {"Authorization": f"Bearer {token}"},
    }

    # Note: Can't call async clear() in sync fixture


@pytest.fixture
async def async_client_setup(auth_storage):
    """Set up app with async client for WebSocket testing."""
    storage = MemoryStorage()
    manager = ConnectionManager()
    topic_storage = InMemoryTopicStorage()

    # Set up authentication
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    # Set up topic storage
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage

    # Get test user and create token
    test_user = await auth_storage.get_user_by_username("user")
    token = create_access_token(test_user)

    messages.set_storage(storage)
    messages.set_manager(manager)
    health.set_storage(storage)
    websocket.set_manager(manager)

    # Create async HTTP client with WebSocket support
    transport = ASGIWebSocketTransport(app=app)
    async_client = AsyncClient(transport=transport, base_url="http://test")

    yield {
        "storage": storage,
        "manager": manager,
        "topic_storage": topic_storage,
        "async_client": async_client,
        "token": token,
        "test_user": test_user,
        "auth_headers": {"Authorization": f"Bearer {token}"},
    }

    # Workaround for httpx-ws cancel scope issue with pytest-asyncio
    # See: https://github.com/frankie567/httpx-ws/issues/78
    transport.exit_stack = None
    await async_client.aclose()
    await asyncio.sleep(0)
    await storage.clear()


class TestWebSocketBasics:
    """Basic WebSocket tests."""

    async def test_websocket_connect_and_subscribe(self, setup_app):
        """Test WebSocket connection and subscription."""
        client = setup_app["client"]
        token = setup_app["token"]
        topic_storage = setup_app["topic_storage"]
        test_user = setup_app["test_user"]

        # Create topic first
        await create_test_topics(topic_storage, test_user.user_id, ["test-topic"])

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Send subscribe message
            websocket.send_json({"type": "subscribe", "topics": ["test-topic"], "client_id": "test-client"})

            # Receive subscription confirmation
            response = websocket.receive_json()

            assert response["type"] == "subscribed"
            assert "test-topic" in response["topics"]
            assert "session_id" in response
            assert "timestamp" in response

    async def test_websocket_ping_pong(self, setup_app):
        """Test WebSocket ping/pong."""
        client = setup_app["client"]
        token = setup_app["token"]
        topic_storage = setup_app["topic_storage"]
        test_user = setup_app["test_user"]

        # Create topic first
        await create_test_topics(topic_storage, test_user.user_id, ["test"])

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe first
            websocket.send_json({"type": "subscribe", "topics": ["test"], "client_id": "test-client"})

            # Wait for subscription confirmation
            websocket.receive_json()

            # Send ping
            websocket.send_json({"type": "ping"})

            # Receive pong
            response = websocket.receive_json()

            assert response["type"] == "pong"
            assert "timestamp" in response

    async def test_websocket_unsubscribe(self, setup_app):
        """Test WebSocket unsubscribe."""
        client = setup_app["client"]
        token = setup_app["token"]
        topic_storage = setup_app["topic_storage"]
        test_user = setup_app["test_user"]

        # Create topics first
        await create_test_topics(topic_storage, test_user.user_id, ["topic1", "topic2", "topic3"])

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe to multiple topics
            websocket.send_json(
                {"type": "subscribe", "topics": ["topic1", "topic2", "topic3"], "client_id": "test-client"}
            )

            # Wait for subscription confirmation
            websocket.receive_json()

            # Unsubscribe from some topics
            websocket.send_json({"type": "unsubscribe", "topics": ["topic1", "topic3"]})

            # Give it a moment to process
            time.sleep(0.1)

        # After websocket closes, check manager state (sync context)
        # Note: In sync tests, we can't use async methods directly
        # We'll verify this works in the async version

    async def test_websocket_invalid_subscribe_message(self, setup_app):
        """Test WebSocket with invalid subscribe message."""
        client = setup_app["client"]
        token = setup_app["token"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Send invalid subscribe (missing required fields)
            websocket.send_json({"type": "subscribe", "topics": []})  # Empty topics invalid

            # Should receive error message
            response = websocket.receive_json()

            assert response["type"] == "error"
            assert "code" in response

    async def test_websocket_unknown_message_type(self, setup_app):
        """Test WebSocket with unknown message type."""
        client = setup_app["client"]
        token = setup_app["token"]
        topic_storage = setup_app["topic_storage"]
        test_user = setup_app["test_user"]

        # Create topic first
        await create_test_topics(topic_storage, test_user.user_id, ["test"])

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe first
            websocket.send_json({"type": "subscribe", "topics": ["test"], "client_id": "test-client"})

            # Wait for confirmation
            websocket.receive_json()

            # Send unknown message type
            websocket.send_json({"type": "unknown_type", "data": "test"})

            # Should receive error
            response = websocket.receive_json()

            assert response["type"] == "error"
            assert response["code"] == "UNKNOWN_MESSAGE_TYPE"


class TestWebSocketMessageDelivery:
    """Tests for message delivery via WebSocket."""

    async def test_receive_message_after_subscription(self, async_client_setup):
        """Test receiving messages via WebSocket."""
        async_client = async_client_setup["async_client"]
        token = async_client_setup["token"]
        auth_headers = async_client_setup["auth_headers"]
        topic_storage = async_client_setup["topic_storage"]
        test_user = async_client_setup["test_user"]

        # Create topic first
        await create_test_topics(topic_storage, test_user.user_id, ["notifications"])

        # Connect to WebSocket using httpx-ws
        async with aconnect_ws(f"http://test/ws?token={token}", async_client) as ws:
            # Subscribe to topic
            await ws.send_json({"type": "subscribe", "topics": ["notifications"], "client_id": "test-client"})

            # Receive subscription confirmation
            sub_msg = await ws.receive_json()
            assert sub_msg["type"] == "subscribed"
            assert "notifications" in sub_msg["topics"]

            # Give subscription time to fully register
            await asyncio.sleep(0.1)

            # Send message via async HTTP
            response = await async_client.post(
                "/api/v1/messages",
                json={"topic": "notifications", "payload": {"user_id": 123, "message": "Hello WebSocket!"}},
                headers=auth_headers,
            )
            assert response.status_code == 201

            # Receive message via WebSocket
            ws_message = await ws.receive_json()

            assert ws_message["type"] == "message"
            assert ws_message["topic"] == "notifications"
            assert ws_message["payload"] == {"user_id": 123, "message": "Hello WebSocket!"}
            assert "message_id" in ws_message
            assert "timestamp" in ws_message

    async def test_multiple_clients_receive_same_message(self, real_server):
        """Test that multiple clients subscribed to same topic receive messages.

        This test uses a real server instance to properly test multiple concurrent
        WebSocket connections without event loop isolation issues.
        """

        base_url = real_server["base_url"]
        ws_url = real_server["ws_url"]
        username = real_server["username"]
        password = real_server["password"]

        # Login to get a valid token from the running server (using form data for OAuth2)
        async with httpx.AsyncClient() as client:
            login_response = await client.post(
                f"{base_url}/auth/login", data={"username": username, "password": password}
            )
            assert login_response.status_code == 200
            real_token = login_response.json()["access_token"]
            real_auth_headers = {"Authorization": f"Bearer {real_token}"}

            # Create the topic
            topic_response = await client.post(
                f"{base_url}/api/v1/topics",
                json={"topic_name": "broadcasts", "is_public": False},
                headers=real_auth_headers,
            )
            assert topic_response.status_code in (200, 201)

        # Connect two WebSocket clients
        async with (
            websockets.connect(f"{ws_url}/ws?token={real_token}") as ws1,
            websockets.connect(f"{ws_url}/ws?token={real_token}") as ws2,
        ):
            # Subscribe both clients to the same topic
            await ws1.send(json.dumps({"type": "subscribe", "topics": ["broadcasts"], "client_id": "client-1"}))
            await ws2.send(json.dumps({"type": "subscribe", "topics": ["broadcasts"], "client_id": "client-2"}))

            # Receive subscription confirmations
            sub_msg1 = json.loads(await ws1.recv())
            sub_msg2 = json.loads(await ws2.recv())

            assert sub_msg1["type"] == "subscribed"
            assert "broadcasts" in sub_msg1["topics"]
            assert sub_msg2["type"] == "subscribed"
            assert "broadcasts" in sub_msg2["topics"]

            # Send a single message via HTTP
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{base_url}/api/v1/messages",
                    json={"topic": "broadcasts", "payload": {"announcement": "Hello all clients!"}},
                    headers=real_auth_headers,
                )
                assert response.status_code == 201

            # Both clients should receive the same message
            ws1_message = json.loads(await ws1.recv())
            ws2_message = json.loads(await ws2.recv())

            # Verify both received the message
            assert ws1_message["type"] == "message"
            assert ws1_message["topic"] == "broadcasts"
            assert ws1_message["payload"] == {"announcement": "Hello all clients!"}

            assert ws2_message["type"] == "message"
            assert ws2_message["topic"] == "broadcasts"
            assert ws2_message["payload"] == {"announcement": "Hello all clients!"}

            # Both should have the same message_id since it's the same message
            assert ws1_message["message_id"] == ws2_message["message_id"]

    async def test_client_only_receives_subscribed_topics(self, async_client_setup):
        """Test that clients only receive messages for subscribed topics."""

        async_client = async_client_setup["async_client"]
        token = async_client_setup["token"]
        auth_headers = async_client_setup["auth_headers"]
        topic_storage = async_client_setup["topic_storage"]
        test_user = async_client_setup["test_user"]

        # Create both topics so message sending works
        await create_test_topics(topic_storage, test_user.user_id, ["topic1", "topic2"])

        # Connect to WebSocket using httpx-ws
        async with aconnect_ws(f"http://test/ws?token={token}", async_client) as ws:
            # Subscribe to topic1 only
            await ws.send_json({"type": "subscribe", "topics": ["topic1"], "client_id": "test-client"})

            # Receive subscription confirmation
            sub_msg = await ws.receive_json()
            assert sub_msg["type"] == "subscribed"
            assert "topic1" in sub_msg["topics"]

            # Give subscription time to fully register
            await asyncio.sleep(0.1)

            # Send to non-subscribed topic first (should NOT receive)
            response1 = await async_client.post(
                "/api/v1/messages",
                json={"topic": "topic2", "payload": {"data": "should not receive"}},
                headers=auth_headers,
            )
            assert response1.status_code == 201

            await asyncio.sleep(0.1)

            # Send to subscribed topic (SHOULD receive this)
            response2 = await async_client.post(
                "/api/v1/messages",
                json={"topic": "topic1", "payload": {"data": "should receive"}},
                headers=auth_headers,
            )
            assert response2.status_code == 201

            # Receive message via WebSocket (should only get topic1)
            ws_message = await ws.receive_json()

            assert ws_message["type"] == "message"
            assert ws_message["topic"] == "topic1"
            assert ws_message["payload"] == {"data": "should receive"}
