"""Tests for ConnectionManager."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import WebSocket

from app.core.connections import ConnectionManager


@pytest.fixture
def manager():
    """Create a ConnectionManager instance."""
    return ConnectionManager()


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket."""
    ws = MagicMock(spec=WebSocket)
    ws.send_json = AsyncMock()
    return ws


@pytest.mark.asyncio
class TestConnectionManager:
    """Tests for ConnectionManager class."""

    async def test_connect_single_topic(self, manager, mock_websocket):
        """Test connecting to a single topic."""
        await manager.connect(mock_websocket, ["test-topic"])

        count = await manager.get_connection_count("test-topic")
        assert count == 1

        total_count = await manager.get_connection_count()
        assert total_count == 1

    async def test_connect_multiple_topics(self, manager, mock_websocket):
        """Test connecting to multiple topics."""
        topics = ["topic1", "topic2", "topic3"]
        await manager.connect(mock_websocket, topics)

        for topic in topics:
            count = await manager.get_connection_count(topic)
            assert count == 1

        client_topics = await manager.get_topics_for_client(mock_websocket)
        assert client_topics == set(topics)

    async def test_disconnect(self, manager, mock_websocket):
        """Test disconnecting a client."""
        await manager.connect(mock_websocket, ["topic1", "topic2"])

        await manager.disconnect(mock_websocket)

        count = await manager.get_connection_count()
        assert count == 0

        topics = await manager.get_all_topics()
        assert len(topics) == 0

    async def test_unsubscribe_from_topics(self, manager, mock_websocket):
        """Test unsubscribing from specific topics."""
        await manager.connect(mock_websocket, ["topic1", "topic2", "topic3"])

        await manager.unsubscribe(mock_websocket, ["topic1", "topic3"])

        client_topics = await manager.get_topics_for_client(mock_websocket)
        assert client_topics == {"topic2"}

        # topic1 and topic3 should have no connections
        assert await manager.get_connection_count("topic1") == 0
        assert await manager.get_connection_count("topic2") == 1
        assert await manager.get_connection_count("topic3") == 0

    async def test_broadcast_to_topic(self, manager):
        """Test broadcasting a message to topic subscribers."""
        # Create multiple mock websockets
        ws1 = MagicMock(spec=WebSocket)
        ws1.send_json = AsyncMock()

        ws2 = MagicMock(spec=WebSocket)
        ws2.send_json = AsyncMock()

        ws3 = MagicMock(spec=WebSocket)
        ws3.send_json = AsyncMock()

        # Connect to topic
        await manager.connect(ws1, ["test-topic"])
        await manager.connect(ws2, ["test-topic"])
        await manager.connect(ws3, ["other-topic"])  # Different topic

        # Broadcast message
        message = {"type": "message", "data": "test"}
        delivered = await manager.broadcast("test-topic", message)

        assert delivered == 2

        # Verify message was sent to correct clients
        ws1.send_json.assert_called_once_with(message)
        ws2.send_json.assert_called_once_with(message)
        ws3.send_json.assert_not_called()  # Different topic

    async def test_broadcast_to_nonexistent_topic(self, manager):
        """Test broadcasting to a topic with no subscribers."""
        message = {"type": "message", "data": "test"}
        delivered = await manager.broadcast("nonexistent", message)

        assert delivered == 0

    async def test_broadcast_handles_dead_connections(self, manager):
        """Test that dead connections are cleaned up during broadcast."""
        ws1 = MagicMock(spec=WebSocket)
        ws1.send_json = AsyncMock()

        ws2 = MagicMock(spec=WebSocket)
        ws2.send_json = AsyncMock(side_effect=Exception("Connection closed"))

        ws3 = MagicMock(spec=WebSocket)
        ws3.send_json = AsyncMock()

        await manager.connect(ws1, ["test-topic"])
        await manager.connect(ws2, ["test-topic"])
        await manager.connect(ws3, ["test-topic"])

        message = {"type": "message", "data": "test"}
        delivered = await manager.broadcast("test-topic", message)

        # Should deliver to 2 out of 3 (ws2 failed)
        assert delivered == 2

        # Dead connection should be removed
        count = await manager.get_connection_count("test-topic")
        assert count == 2

    async def test_multiple_clients_multiple_topics(self, manager):
        """Test complex scenario with multiple clients and topics."""
        ws1 = MagicMock(spec=WebSocket)
        ws1.send_json = AsyncMock()

        ws2 = MagicMock(spec=WebSocket)
        ws2.send_json = AsyncMock()

        ws3 = MagicMock(spec=WebSocket)
        ws3.send_json = AsyncMock()

        # Client 1: topic1, topic2
        await manager.connect(ws1, ["topic1", "topic2"])

        # Client 2: topic2, topic3
        await manager.connect(ws2, ["topic2", "topic3"])

        # Client 3: topic1
        await manager.connect(ws3, ["topic1"])

        # Check connection counts
        assert await manager.get_connection_count("topic1") == 2  # ws1, ws3
        assert await manager.get_connection_count("topic2") == 2  # ws1, ws2
        assert await manager.get_connection_count("topic3") == 1  # ws2
        assert await manager.get_connection_count() == 3  # Total clients

        # Broadcast to topic1
        message1 = {"type": "message", "topic": "topic1"}
        delivered = await manager.broadcast("topic1", message1)
        assert delivered == 2

        # Broadcast to topic2
        message2 = {"type": "message", "topic": "topic2"}
        delivered = await manager.broadcast("topic2", message2)
        assert delivered == 2

        # Disconnect ws1
        await manager.disconnect(ws1)

        assert await manager.get_connection_count("topic1") == 1  # Only ws3
        assert await manager.get_connection_count("topic2") == 1  # Only ws2
        assert await manager.get_connection_count() == 2  # ws2, ws3

    async def test_get_all_topics(self, manager, mock_websocket):
        """Test getting all active topics."""
        await manager.connect(mock_websocket, ["topic1", "topic2", "topic3"])

        all_topics = await manager.get_all_topics()
        assert all_topics == {"topic1", "topic2", "topic3"}

    async def test_get_topics_for_nonexistent_client(self, manager, mock_websocket):
        """Test getting topics for a client that hasn't connected."""
        topics = await manager.get_topics_for_client(mock_websocket)
        assert topics == set()

    async def test_disconnect_nonexistent_client(self, manager, mock_websocket):
        """Test disconnecting a client that was never connected."""
        # Should not raise an exception
        await manager.disconnect(mock_websocket)

        count = await manager.get_connection_count()
        assert count == 0

    async def test_concurrent_connections(self, manager):
        """Test thread safety with concurrent connections."""
        import asyncio

        websockets = [MagicMock(spec=WebSocket) for _ in range(10)]
        for ws in websockets:
            ws.send_json = AsyncMock()

        # Connect all websockets concurrently
        tasks = [manager.connect(ws, [f"topic{i % 3}"]) for i, ws in enumerate(websockets)]
        await asyncio.gather(*tasks)

        # Verify all connections were added
        total_count = await manager.get_connection_count()
        assert total_count == 10

        # Verify topic distribution
        topic0_count = await manager.get_connection_count("topic0")
        topic1_count = await manager.get_connection_count("topic1")
        topic2_count = await manager.get_connection_count("topic2")

        assert topic0_count + topic1_count + topic2_count == 10
