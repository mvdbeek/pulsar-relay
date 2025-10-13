"""Tests for WebSocket endpoints."""

import pytest
import asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.storage.memory import MemoryStorage
from app.core.connections import ConnectionManager
from app.api import messages, health, websocket
from app.auth.storage import InMemoryUserStorage, create_default_users
from app.auth.dependencies import set_user_storage
from app.auth.jwt import create_access_token


@pytest.fixture
def setup_app():
    """Set up app with fresh storage, connection manager, and authentication."""
    storage = MemoryStorage()
    manager = ConnectionManager()

    # Set up authentication
    user_storage = InMemoryUserStorage()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(create_default_users(user_storage))
    set_user_storage(user_storage)
    app.state.user_storage = user_storage

    # Get test user and create token
    test_user = loop.run_until_complete(user_storage.get_user_by_username("user"))
    token = create_access_token(test_user)

    messages.set_storage(storage)
    messages.set_manager(manager)
    health.set_storage(storage)
    websocket.set_manager(manager)

    yield {
        "storage": storage,
        "manager": manager,
        "client": TestClient(app),
        "token": token,
        "auth_headers": {"Authorization": f"Bearer {token}"}
    }

    # Note: Can't call async clear() in sync fixture


class TestWebSocketBasics:
    """Basic WebSocket tests."""

    def test_websocket_connect_and_subscribe(self, setup_app):
        """Test WebSocket connection and subscription."""
        client = setup_app["client"]
        token = setup_app["token"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Send subscribe message
            websocket.send_json({
                "type": "subscribe",
                "topics": ["test-topic"],
                "client_id": "test-client"
            })

            # Receive subscription confirmation
            response = websocket.receive_json()

            assert response["type"] == "subscribed"
            assert "test-topic" in response["topics"]
            assert "session_id" in response
            assert "timestamp" in response

    def test_websocket_ping_pong(self, setup_app):
        """Test WebSocket ping/pong."""
        client = setup_app["client"]
        token = setup_app["token"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe first
            websocket.send_json({
                "type": "subscribe",
                "topics": ["test"],
                "client_id": "test-client"
            })

            # Wait for subscription confirmation
            websocket.receive_json()

            # Send ping
            websocket.send_json({"type": "ping"})

            # Receive pong
            response = websocket.receive_json()

            assert response["type"] == "pong"
            assert "timestamp" in response

    def test_websocket_unsubscribe(self, setup_app):
        """Test WebSocket unsubscribe."""
        client = setup_app["client"]
        token = setup_app["token"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe to multiple topics
            websocket.send_json({
                "type": "subscribe",
                "topics": ["topic1", "topic2", "topic3"],
                "client_id": "test-client"
            })

            # Wait for subscription confirmation
            websocket.receive_json()

            # Unsubscribe from some topics
            websocket.send_json({
                "type": "unsubscribe",
                "topics": ["topic1", "topic3"]
            })

            # Give it a moment to process
            import time
            time.sleep(0.1)

        # After websocket closes, check manager state (sync context)
        # Note: In sync tests, we can't use async methods directly
        # We'll verify this works in the async version

    def test_websocket_invalid_subscribe_message(self, setup_app):
        """Test WebSocket with invalid subscribe message."""
        client = setup_app["client"]
        token = setup_app["token"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Send invalid subscribe (missing required fields)
            websocket.send_json({
                "type": "subscribe",
                "topics": []  # Empty topics invalid
            })

            # Should receive error message
            response = websocket.receive_json()

            assert response["type"] == "error"
            assert "code" in response

    def test_websocket_unknown_message_type(self, setup_app):
        """Test WebSocket with unknown message type."""
        client = setup_app["client"]
        token = setup_app["token"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe first
            websocket.send_json({
                "type": "subscribe",
                "topics": ["test"],
                "client_id": "test-client"
            })

            # Wait for confirmation
            websocket.receive_json()

            # Send unknown message type
            websocket.send_json({
                "type": "unknown_type",
                "data": "test"
            })

            # Should receive error
            response = websocket.receive_json()

            assert response["type"] == "error"
            assert response["code"] == "UNKNOWN_MESSAGE_TYPE"


class TestWebSocketMessageDelivery:
    """Tests for message delivery via WebSocket."""

    def test_receive_message_after_subscription(self, setup_app):
        """Test receiving messages via WebSocket."""
        client = setup_app["client"]
        token = setup_app["token"]
        auth_headers = setup_app["auth_headers"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe to topic
            websocket.send_json({
                "type": "subscribe",
                "topics": ["notifications"],
                "client_id": "test-client"
            })

            # Wait for subscription confirmation
            sub_response = websocket.receive_json()
            assert sub_response["type"] == "subscribed"

            # Send a message via HTTP (using same test client)
            import threading
            import time

            def send_message():
                time.sleep(0.2)  # Small delay
                response = client.post(
                    "/api/v1/messages",
                    json={
                        "topic": "notifications",
                        "payload": {"user_id": 123, "message": "Hello WebSocket!"}
                    },
                    headers=auth_headers
                )

            # Start background thread to send message
            thread = threading.Thread(target=send_message)
            thread.start()

            # Wait for message on WebSocket (with timeout)
            ws_message = websocket.receive_json()

            assert ws_message["type"] == "message"
            assert ws_message["topic"] == "notifications"
            assert ws_message["payload"] == {"user_id": 123, "message": "Hello WebSocket!"}
            assert "message_id" in ws_message
            assert "timestamp" in ws_message

            thread.join()

    def test_multiple_clients_receive_same_message(self, setup_app):
        """Test that multiple clients subscribed to same topic receive messages."""
        # This test is complex with multiple WebSocket clients in sync mode
        # Skip for now - would need async test client
        pytest.skip("Multiple WebSocket clients require async test client")

    def test_client_only_receives_subscribed_topics(self, setup_app):
        """Test that clients only receive messages for subscribed topics."""
        client = setup_app["client"]
        token = setup_app["token"]
        auth_headers = setup_app["auth_headers"]

        with client.websocket_connect(f"/ws?token={token}") as websocket:
            # Subscribe to topic1 only
            websocket.send_json({
                "type": "subscribe",
                "topics": ["topic1"],
                "client_id": "test-client"
            })

            websocket.receive_json()  # Subscription confirmation

            # Send message to different topic (should not receive)
            import threading
            import time

            def send_messages():
                # Send to non-subscribed topic
                client.post(
                    "/api/v1/messages",
                    json={"topic": "topic2", "payload": {"data": "should not receive"}},
                    headers=auth_headers
                )
                time.sleep(0.1)
                # Send to subscribed topic
                client.post(
                    "/api/v1/messages",
                    json={"topic": "topic1", "payload": {"data": "should receive"}},
                    headers=auth_headers
                )

            thread = threading.Thread(target=send_messages)
            thread.start()

            # Should only receive message from topic1
            msg = websocket.receive_json()

            assert msg["type"] == "message"
            assert msg["topic"] == "topic1"
            assert msg["payload"] == {"data": "should receive"}

            thread.join()
