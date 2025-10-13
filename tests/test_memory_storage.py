"""Tests for in-memory storage backend."""

import datetime

import pytest

from app.storage.memory import MemoryStorage


@pytest.mark.asyncio
class TestMemoryStorage:
    """Tests for MemoryStorage class."""

    async def test_save_and_get_messages(self):
        """Test saving and retrieving messages."""
        storage = MemoryStorage()

        # Save messages
        await storage.save_message(
            "msg_1",
            "test-topic",
            {"data": "value1"},
            datetime.datetime.now(datetime.UTC),
            {"key": "value"},
        )
        await storage.save_message("msg_2", "test-topic", {"data": "value2"}, datetime.datetime.now(datetime.UTC))

        # Retrieve messages
        messages = await storage.get_messages("test-topic")

        assert len(messages) == 2
        assert messages[0]["message_id"] == "msg_1"
        assert messages[0]["payload"] == {"data": "value1"}
        assert messages[0]["metadata"] == {"key": "value"}
        assert messages[1]["message_id"] == "msg_2"
        assert messages[1]["payload"] == {"data": "value2"}

    async def test_get_messages_with_limit(self):
        """Test getting messages with limit."""
        storage = MemoryStorage()

        # Save multiple messages
        for i in range(5):
            await storage.save_message(f"msg_{i}", "test-topic", {"index": i}, datetime.datetime.now(datetime.UTC))

        # Get with limit
        messages = await storage.get_messages("test-topic", limit=3)

        assert len(messages) == 3
        assert messages[0]["message_id"] == "msg_0"
        assert messages[2]["message_id"] == "msg_2"

    async def test_get_messages_since(self):
        """Test getting messages since a specific message ID."""
        storage = MemoryStorage()

        # Save messages
        for i in range(5):
            await storage.save_message(f"msg_{i}", "test-topic", {"index": i}, datetime.datetime.now(datetime.UTC))

        # Get messages since msg_2
        messages = await storage.get_messages("test-topic", since="msg_2")

        assert len(messages) == 2
        assert messages[0]["message_id"] == "msg_3"
        assert messages[1]["message_id"] == "msg_4"

    async def test_get_messages_nonexistent_topic(self):
        """Test getting messages from nonexistent topic."""
        storage = MemoryStorage()

        messages = await storage.get_messages("nonexistent")

        assert messages == []

    async def test_trim_topic(self):
        """Test trimming old messages from a topic."""
        storage = MemoryStorage()

        # Save 10 messages
        for i in range(10):
            await storage.save_message(f"msg_{i}", "test-topic", {"index": i}, datetime.datetime.now(datetime.UTC))

        # Trim to 5 messages
        removed = await storage.trim_topic("test-topic", 5)

        assert removed == 5
        assert await storage.get_topic_length("test-topic") == 5

        # Verify oldest messages were removed
        messages = await storage.get_messages("test-topic", limit=100)
        assert messages[0]["message_id"] == "msg_5"
        assert messages[4]["message_id"] == "msg_9"

    async def test_trim_topic_no_effect(self):
        """Test trimming when topic has fewer messages than max."""
        storage = MemoryStorage()

        # Save 3 messages
        for i in range(3):
            await storage.save_message(f"msg_{i}", "test-topic", {"index": i}, datetime.datetime.now(datetime.UTC))

        # Try to trim to 10 messages
        removed = await storage.trim_topic("test-topic", 10)

        assert removed == 0
        assert await storage.get_topic_length("test-topic") == 3

    async def test_get_topic_length(self):
        """Test getting topic length."""
        storage = MemoryStorage()

        assert await storage.get_topic_length("test-topic") == 0

        await storage.save_message("msg_1", "test-topic", {"data": 1}, datetime.datetime.now(datetime.UTC))
        assert await storage.get_topic_length("test-topic") == 1

        await storage.save_message("msg_2", "test-topic", {"data": 2}, datetime.datetime.now(datetime.UTC))
        assert await storage.get_topic_length("test-topic") == 2

    async def test_max_messages_per_topic(self):
        """Test that deque automatically trims when max_messages is reached."""
        storage = MemoryStorage(max_messages_per_topic=5)

        # Save 10 messages
        for i in range(10):
            await storage.save_message(f"msg_{i}", "test-topic", {"index": i}, datetime.datetime.now(datetime.UTC))

        # Should only have last 5 messages
        length = await storage.get_topic_length("test-topic")
        assert length == 5

        messages = await storage.get_messages("test-topic", limit=100)
        assert messages[0]["message_id"] == "msg_5"
        assert messages[4]["message_id"] == "msg_9"

    async def test_multiple_topics(self):
        """Test storing messages in multiple topics."""
        storage = MemoryStorage()

        await storage.save_message("msg_1", "topic1", {"data": 1}, datetime.datetime.now(datetime.UTC))
        await storage.save_message("msg_2", "topic2", {"data": 2}, datetime.datetime.now(datetime.UTC))
        await storage.save_message("msg_3", "topic1", {"data": 3}, datetime.datetime.now(datetime.UTC))

        topic1_messages = await storage.get_messages("topic1")
        topic2_messages = await storage.get_messages("topic2")

        assert len(topic1_messages) == 2
        assert len(topic2_messages) == 1
        assert topic1_messages[0]["payload"] == {"data": 1}
        assert topic2_messages[0]["payload"] == {"data": 2}

    async def test_health_check(self):
        """Test health check."""
        storage = MemoryStorage()
        assert await storage.health_check() is True

    async def test_close(self):
        """Test close (no-op for memory storage)."""
        storage = MemoryStorage()
        await storage.close()  # Should not raise

    async def test_clear(self):
        """Test clearing all messages."""
        storage = MemoryStorage()

        await storage.save_message("msg_1", "topic1", {"data": 1}, datetime.datetime.now(datetime.UTC))
        await storage.save_message("msg_2", "topic2", {"data": 2}, datetime.datetime.now(datetime.UTC))

        await storage.clear()

        assert await storage.get_topic_length("topic1") == 0
        assert await storage.get_topic_length("topic2") == 0
