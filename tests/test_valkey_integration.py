"""Integration tests for Valkey storage backend.

These tests require a running Valkey instance on localhost:6379.
Start Valkey with: docker run -d -p 6379:6379 valkey/valkey:latest

Run these tests with: pytest tests/test_valkey_integration.py -v
"""

import asyncio
import os
from datetime import datetime, timedelta

import pytest

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
        max_messages_per_topic=100,
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


class TestValkeyIntegrationBasics:
    """Basic integration tests for Valkey storage."""

    @pytest.mark.asyncio
    async def test_connection_and_disconnection(self):
        """Test connecting and disconnecting from Valkey."""
        storage = ValkeyStorage(host="localhost", port=6379)

        # Test connection
        await storage.connect()
        assert storage._connected is True

        # Verify we can ping
        health = await storage.health_check()
        assert health["status"] == "healthy"
        assert health["connected"] is True

        # Test disconnection
        await storage.disconnect()
        assert storage._connected is False

    @pytest.mark.asyncio
    async def test_save_and_retrieve_single_message(self, valkey_storage):
        """Test saving and retrieving a single message."""
        timestamp = datetime(2025, 1, 1, 12, 0, 0)

        # Save message
        await valkey_storage.save_message(
            message_id="msg_integration_1",
            topic="test-topic",
            payload={"data": "test value", "number": 42},
            timestamp=timestamp,
            metadata={"source": "integration-test"},
        )

        # Retrieve messages
        messages = await valkey_storage.get_messages("test-topic")

        assert len(messages) == 1
        assert messages[0]["message_id"] == "msg_integration_1"
        assert messages[0]["topic"] == "test-topic"
        assert messages[0]["payload"] == {"data": "test value", "number": 42}
        assert messages[0]["metadata"] == {"source": "integration-test"}
        assert "stream_id" in messages[0]

    @pytest.mark.asyncio
    async def test_save_multiple_messages_same_topic(self, valkey_storage):
        """Test saving multiple messages to the same topic."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Save 10 messages
        for i in range(10):
            await valkey_storage.save_message(
                message_id=f"msg_{i}",
                topic="multi-test",
                payload={"index": i, "data": f"message_{i}"},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Retrieve all messages
        messages = await valkey_storage.get_messages("multi-test", limit=20)

        assert len(messages) == 10

        # Verify messages are in order
        for i, msg in enumerate(messages):
            assert msg["message_id"] == f"msg_{i}"
            assert msg["payload"]["index"] == i

    @pytest.mark.asyncio
    async def test_multiple_topics(self, valkey_storage):
        """Test saving messages to multiple different topics."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Save messages to different topics
        topics = ["topic-a", "topic-b", "topic-c"]

        for topic_idx, topic in enumerate(topics):
            for msg_idx in range(5):
                await valkey_storage.save_message(
                    message_id=f"msg_{topic}_{msg_idx}",
                    topic=topic,
                    payload={"topic": topic, "index": msg_idx},
                    timestamp=base_time + timedelta(seconds=msg_idx),
                )

        # Verify each topic has correct messages
        for topic in topics:
            messages = await valkey_storage.get_messages(topic)
            assert len(messages) == 5

            for msg in messages:
                assert msg["topic"] == topic
                assert msg["payload"]["topic"] == topic


class TestValkeyIntegrationPagination:
    """Test pagination and stream ID handling."""

    @pytest.mark.asyncio
    async def test_pagination_with_limit(self, valkey_storage):
        """Test retrieving messages with pagination."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Save 20 messages
        for i in range(20):
            await valkey_storage.save_message(
                message_id=f"msg_{i:02d}",
                topic="pagination-test",
                payload={"index": i},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Get first page (10 messages)
        page1 = await valkey_storage.get_messages("pagination-test", limit=10)
        assert len(page1) == 10
        assert page1[0]["payload"]["index"] == 0
        assert page1[9]["payload"]["index"] == 9

        # Get second page using last stream ID
        last_stream_id = page1[-1]["stream_id"]
        page2 = await valkey_storage.get_messages("pagination-test", since=last_stream_id, limit=10)
        assert len(page2) == 10
        assert page2[0]["payload"]["index"] == 10
        assert page2[9]["payload"]["index"] == 19

    @pytest.mark.asyncio
    async def test_pagination_beyond_available_messages(self, valkey_storage):
        """Test pagination when requesting more messages than available."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Save only 5 messages
        for i in range(5):
            await valkey_storage.save_message(
                message_id=f"msg_{i}",
                topic="limited-topic",
                payload={"index": i},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Request 100 messages
        messages = await valkey_storage.get_messages("limited-topic", limit=100)

        # Should only get 5
        assert len(messages) == 5


class TestValkeyIntegrationTrimming:
    """Test stream trimming functionality."""

    @pytest.mark.asyncio
    async def test_automatic_trimming_on_save(self, valkey_storage):
        """Test that streams are automatically trimmed when exceeding max length."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Storage is configured with max_messages_per_topic=100
        # Save 150 messages
        for i in range(150):
            await valkey_storage.save_message(
                message_id=f"msg_{i:03d}",
                topic="trim-test",
                payload={"index": i},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Check topic length - should be exactly 100 (exact trimming after each save)
        length = await valkey_storage.get_topic_length("trim-test")
        assert length == 100

    @pytest.mark.asyncio
    async def test_manual_trimming(self, valkey_storage):
        """Test manually trimming a topic."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Save 50 messages
        for i in range(50):
            await valkey_storage.save_message(
                message_id=f"msg_{i}",
                topic="manual-trim",
                payload={"index": i},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Verify we have 50 messages
        initial_length = await valkey_storage.get_topic_length("manual-trim")
        assert initial_length == 50

        # Trim to keep only 20
        removed = await valkey_storage.trim_topic("manual-trim", keep_count=20)
        assert removed == 30

        # Verify we now have 20 messages
        final_length = await valkey_storage.get_topic_length("manual-trim")
        assert final_length == 20

        # Verify we kept the most recent messages
        messages = await valkey_storage.get_messages("manual-trim", limit=20)
        assert len(messages) == 20
        # Due to trimming from the beginning, we should have messages 30-49
        assert messages[0]["payload"]["index"] >= 30

    @pytest.mark.asyncio
    async def test_trim_topic_no_trim_needed(self, valkey_storage):
        """Test trimming when topic has fewer messages than keep_count."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Save only 10 messages
        for i in range(10):
            await valkey_storage.save_message(
                message_id=f"msg_{i}",
                topic="small-topic",
                payload={"index": i},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Try to trim keeping 50 messages
        removed = await valkey_storage.trim_topic("small-topic", keep_count=50)
        assert removed == 0

        # Verify we still have all 10 messages
        length = await valkey_storage.get_topic_length("small-topic")
        assert length == 10


class TestValkeyIntegrationPerformance:
    """Test performance and concurrent operations."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_same_topic(self, valkey_storage):
        """Test writing to the same topic concurrently."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        async def write_message(i: int):
            await valkey_storage.save_message(
                message_id=f"concurrent_msg_{i}",
                topic="concurrent-writes",
                payload={"index": i},
                timestamp=base_time + timedelta(milliseconds=i),
            )

        # Write 50 messages concurrently
        await asyncio.gather(*[write_message(i) for i in range(50)])

        # Verify all messages were saved
        length = await valkey_storage.get_topic_length("concurrent-writes")
        assert length == 50

    @pytest.mark.asyncio
    async def test_concurrent_writes_different_topics(self, valkey_storage):
        """Test writing to different topics concurrently."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        async def write_to_topic(topic_idx: int):
            for msg_idx in range(10):
                await valkey_storage.save_message(
                    message_id=f"msg_{topic_idx}_{msg_idx}",
                    topic=f"concurrent-topic-{topic_idx}",
                    payload={"topic_idx": topic_idx, "msg_idx": msg_idx},
                    timestamp=base_time + timedelta(milliseconds=msg_idx),
                )

        # Write to 10 topics concurrently
        await asyncio.gather(*[write_to_topic(i) for i in range(10)])

        # Verify each topic has 10 messages
        for i in range(10):
            length = await valkey_storage.get_topic_length(f"concurrent-topic-{i}")
            assert length == 10

    @pytest.mark.asyncio
    async def test_large_payload(self, valkey_storage):
        """Test handling large message payloads."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Create a large payload (100KB of data)
        large_data = "x" * 100_000

        await valkey_storage.save_message(
            message_id="large_msg",
            topic="large-payload",
            payload={"data": large_data, "size": len(large_data)},
            timestamp=base_time,
        )

        # Retrieve and verify
        messages = await valkey_storage.get_messages("large-payload")
        assert len(messages) == 1
        assert len(messages[0]["payload"]["data"]) == 100_000
        assert messages[0]["payload"]["size"] == 100_000


class TestValkeyIntegrationEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_empty_topic(self, valkey_storage):
        """Test retrieving from a non-existent/empty topic."""
        messages = await valkey_storage.get_messages("non-existent-topic")
        assert messages == []

    @pytest.mark.asyncio
    async def test_topic_length_nonexistent(self, valkey_storage):
        """Test getting length of non-existent topic."""
        length = await valkey_storage.get_topic_length("does-not-exist")
        assert length == 0

    @pytest.mark.asyncio
    async def test_message_without_metadata(self, valkey_storage):
        """Test saving and retrieving message without metadata."""
        await valkey_storage.save_message(
            message_id="no_meta",
            topic="no-metadata",
            payload={"data": "test"},
            timestamp=datetime(2025, 1, 1, 12, 0, 0),
            metadata=None,
        )

        messages = await valkey_storage.get_messages("no-metadata")
        assert len(messages) == 1
        assert messages[0]["metadata"] == {}

    @pytest.mark.asyncio
    async def test_special_characters_in_topic_name(self, valkey_storage):
        """Test topics with special characters."""
        special_topics = [
            "topic-with-dashes",
            "topic_with_underscores",
            "topic.with.dots",
            "topic:with:colons",
        ]

        base_time = datetime(2025, 1, 1, 12, 0, 0)

        for topic in special_topics:
            await valkey_storage.save_message(
                message_id=f"msg_{topic}",
                topic=topic,
                payload={"topic": topic},
                timestamp=base_time,
            )

        # Verify all topics work
        for topic in special_topics:
            messages = await valkey_storage.get_messages(topic)
            assert len(messages) == 1
            assert messages[0]["payload"]["topic"] == topic

    @pytest.mark.asyncio
    async def test_unicode_in_payload(self, valkey_storage):
        """Test handling Unicode characters in payload."""
        await valkey_storage.save_message(
            message_id="unicode_msg",
            topic="unicode-test",
            payload={
                "text": "Hello 世界 🌍",
                "emoji": "🚀💻🔥",
                "special": "café résumé naïve",
            },
            timestamp=datetime(2025, 1, 1, 12, 0, 0),
        )

        messages = await valkey_storage.get_messages("unicode-test")
        assert len(messages) == 1
        assert messages[0]["payload"]["text"] == "Hello 世界 🌍"
        assert messages[0]["payload"]["emoji"] == "🚀💻🔥"
        assert messages[0]["payload"]["special"] == "café résumé naïve"


class TestValkeyIntegrationHealthAndMonitoring:
    """Test health checks and monitoring functionality."""

    @pytest.mark.asyncio
    async def test_health_check(self, valkey_storage):
        """Test health check returns correct information."""
        health = await valkey_storage.health_check()

        assert health["status"] == "healthy"
        assert health["connected"] is True
        assert health["host"] == "localhost"
        assert health["port"] == 6379

    @pytest.mark.asyncio
    async def test_health_check_after_operations(self, valkey_storage):
        """Test health check after performing operations."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Perform some operations
        for i in range(10):
            await valkey_storage.save_message(
                message_id=f"msg_{i}",
                topic="health-test",
                payload={"index": i},
                timestamp=base_time + timedelta(seconds=i),
            )

        # Health check should still be healthy
        health = await valkey_storage.health_check()
        assert health["status"] == "healthy"
        assert health["connected"] is True
        assert health["host"] == "localhost"
        assert health["port"] == 6379


class TestValkeyIntegrationCleanup:
    """Test cleanup and clear operations."""

    @pytest.mark.asyncio
    async def test_clear_all_topics(self, valkey_storage):
        """Test clearing all topics."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        # Create messages in multiple topics
        for topic_idx in range(5):
            topic = f"clear-test-{topic_idx}"
            for msg_idx in range(10):
                await valkey_storage.save_message(
                    message_id=f"msg_{topic_idx}_{msg_idx}",
                    topic=topic,
                    payload={"index": msg_idx},
                    timestamp=base_time + timedelta(seconds=msg_idx),
                )

        # Verify topics exist
        for topic_idx in range(5):
            length = await valkey_storage.get_topic_length(f"clear-test-{topic_idx}")
            assert length == 10

        # Clear all topics
        await valkey_storage.clear()

        # Verify all topics are cleared
        for topic_idx in range(5):
            length = await valkey_storage.get_topic_length(f"clear-test-{topic_idx}")
            assert length == 0


@pytest.mark.asyncio
async def test_connection_failure_handling():
    """Test handling of connection failures."""
    # Try to connect to non-existent Valkey instance
    storage = ValkeyStorage(host="localhost", port=9999)

    with pytest.raises(Exception):
        await storage.connect()


@pytest.mark.asyncio
async def test_full_workflow():
    """Test a complete workflow: connect, write, read, trim, disconnect."""
    storage = ValkeyStorage(host="localhost", port=6379, max_messages_per_topic=50)

    try:
        # Connect
        await storage.connect()
        assert storage._connected is True

        # Clear any existing data
        await storage.clear()

        # Write messages
        base_time = datetime(2025, 1, 1, 12, 0, 0)
        for i in range(30):
            await storage.save_message(
                message_id=f"workflow_msg_{i}",
                topic="workflow-test",
                payload={"index": i, "data": f"message {i}"},
                timestamp=base_time + timedelta(seconds=i),
                metadata={"batch": i // 10},
            )

        # Verify length
        length = await storage.get_topic_length("workflow-test")
        assert length == 30

        # Read messages
        messages = await storage.get_messages("workflow-test", limit=10)
        assert len(messages) == 10

        # Read next page
        last_id = messages[-1]["stream_id"]
        next_messages = await storage.get_messages("workflow-test", since=last_id, limit=10)
        assert len(next_messages) == 10

        # Trim
        removed = await storage.trim_topic("workflow-test", keep_count=15)
        assert removed == 15

        new_length = await storage.get_topic_length("workflow-test")
        assert new_length == 15

        # Health check
        health = await storage.health_check()
        assert health["status"] == "healthy"

        # Clear
        await storage.clear()

        final_length = await storage.get_topic_length("workflow-test")
        assert final_length == 0

    finally:
        # Disconnect
        await storage.disconnect()
        assert storage._connected is False
