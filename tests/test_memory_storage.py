"""Tests for in-memory storage backend."""

import datetime

import pytest

from pulsar_relay.storage.memory import MemoryStorage

# Topic storage is namespaced by ``(owner_id, topic_name)`` since Phase
# 3c (closes API H#5). These tests pin one owner; cross-owner isolation
# is exercised in tests/test_topic_namespacing.py.
OWNER = "u-test"


@pytest.mark.anyio
class TestMemoryStorage:
    """Tests for MemoryStorage class."""

    async def test_save_and_get_messages(self):
        storage = MemoryStorage()

        msg_id_1 = await storage.save_message(
            OWNER,
            "test-topic",
            {"data": "value1"},
            datetime.datetime.now(datetime.timezone.utc),
            {"key": "value"},
        )
        msg_id_2 = await storage.save_message(
            OWNER, "test-topic", {"data": "value2"}, datetime.datetime.now(datetime.timezone.utc)
        )

        assert msg_id_1.startswith("msg_")
        assert msg_id_2.startswith("msg_")
        assert msg_id_1 != msg_id_2

        messages = await storage.get_messages(OWNER, "test-topic")

        assert len(messages) == 2
        assert messages[0]["message_id"] == msg_id_1
        assert messages[0]["payload"] == {"data": "value1"}
        assert messages[0]["metadata"] == {"key": "value"}
        assert messages[1]["message_id"] == msg_id_2
        assert messages[1]["payload"] == {"data": "value2"}

    async def test_get_messages_with_limit(self):
        storage = MemoryStorage()

        msg_ids = []
        for i in range(5):
            msg_id = await storage.save_message(
                OWNER, "test-topic", {"index": i}, datetime.datetime.now(datetime.timezone.utc)
            )
            msg_ids.append(msg_id)

        messages = await storage.get_messages(OWNER, "test-topic", limit=3)

        assert len(messages) == 3
        assert messages[0]["message_id"] == msg_ids[0]
        assert messages[2]["message_id"] == msg_ids[2]

    async def test_get_messages_since(self):
        storage = MemoryStorage()

        msg_ids = []
        for i in range(5):
            msg_id = await storage.save_message(
                OWNER, "test-topic", {"index": i}, datetime.datetime.now(datetime.timezone.utc)
            )
            msg_ids.append(msg_id)

        messages = await storage.get_messages(OWNER, "test-topic", since=msg_ids[2])

        assert len(messages) == 2
        assert messages[0]["message_id"] == msg_ids[3]
        assert messages[1]["message_id"] == msg_ids[4]

    async def test_get_messages_nonexistent_topic(self):
        storage = MemoryStorage()
        messages = await storage.get_messages(OWNER, "nonexistent")
        assert messages == []

    async def test_trim_topic(self):
        storage = MemoryStorage()

        msg_ids = []
        for i in range(10):
            msg_id = await storage.save_message(
                OWNER, "test-topic", {"index": i}, datetime.datetime.now(datetime.timezone.utc)
            )
            msg_ids.append(msg_id)

        removed = await storage.trim_topic(OWNER, "test-topic", 5)
        assert removed == 5
        assert await storage.get_topic_length(OWNER, "test-topic") == 5

        messages = await storage.get_messages(OWNER, "test-topic", limit=100)
        assert messages[0]["message_id"] == msg_ids[5]
        assert messages[4]["message_id"] == msg_ids[9]

    async def test_trim_topic_no_effect(self):
        storage = MemoryStorage()

        for i in range(3):
            await storage.save_message(OWNER, "test-topic", {"index": i}, datetime.datetime.now(datetime.timezone.utc))

        removed = await storage.trim_topic(OWNER, "test-topic", 10)
        assert removed == 0
        assert await storage.get_topic_length(OWNER, "test-topic") == 3

    async def test_get_topic_length(self):
        storage = MemoryStorage()

        assert await storage.get_topic_length(OWNER, "test-topic") == 0

        await storage.save_message(OWNER, "test-topic", {"data": 1}, datetime.datetime.now(datetime.timezone.utc))
        assert await storage.get_topic_length(OWNER, "test-topic") == 1

        await storage.save_message(OWNER, "test-topic", {"data": 2}, datetime.datetime.now(datetime.timezone.utc))
        assert await storage.get_topic_length(OWNER, "test-topic") == 2

    async def test_max_messages_per_topic(self):
        storage = MemoryStorage(max_messages_per_topic=5)

        msg_ids = []
        for i in range(10):
            msg_id = await storage.save_message(
                OWNER, "test-topic", {"index": i}, datetime.datetime.now(datetime.timezone.utc)
            )
            msg_ids.append(msg_id)

        length = await storage.get_topic_length(OWNER, "test-topic")
        assert length == 5

        messages = await storage.get_messages(OWNER, "test-topic", limit=100)
        assert messages[0]["message_id"] == msg_ids[5]
        assert messages[4]["message_id"] == msg_ids[9]

    async def test_multiple_topics(self):
        storage = MemoryStorage()

        await storage.save_message(OWNER, "topic1", {"data": 1}, datetime.datetime.now(datetime.timezone.utc))
        await storage.save_message(OWNER, "topic2", {"data": 2}, datetime.datetime.now(datetime.timezone.utc))
        await storage.save_message(OWNER, "topic1", {"data": 3}, datetime.datetime.now(datetime.timezone.utc))

        topic1_messages = await storage.get_messages(OWNER, "topic1")
        topic2_messages = await storage.get_messages(OWNER, "topic2")

        assert len(topic1_messages) == 2
        assert len(topic2_messages) == 1
        assert topic1_messages[0]["payload"] == {"data": 1}
        assert topic2_messages[0]["payload"] == {"data": 2}

    async def test_two_owners_same_topic_name_dont_collide(self):
        """The core API H#5 invariant: user A's "jobs" and user B's
        "jobs" are independent streams in storage."""
        storage = MemoryStorage()
        owner_a, owner_b = "alice", "bob"

        await storage.save_message(owner_a, "jobs", {"from": "alice-1"}, datetime.datetime.now(datetime.timezone.utc))
        await storage.save_message(owner_b, "jobs", {"from": "bob-1"}, datetime.datetime.now(datetime.timezone.utc))
        await storage.save_message(owner_b, "jobs", {"from": "bob-2"}, datetime.datetime.now(datetime.timezone.utc))

        alice_msgs = await storage.get_messages(owner_a, "jobs")
        bob_msgs = await storage.get_messages(owner_b, "jobs")

        assert [m["payload"] for m in alice_msgs] == [{"from": "alice-1"}]
        assert [m["payload"] for m in bob_msgs] == [{"from": "bob-1"}, {"from": "bob-2"}]

    async def test_health_check(self):
        storage = MemoryStorage()
        assert await storage.health_check() == {"status": "healthy"}

    async def test_close(self):
        storage = MemoryStorage()
        await storage.close()

    async def test_clear(self):
        storage = MemoryStorage()

        await storage.save_message(OWNER, "topic1", {"data": 1}, datetime.datetime.now(datetime.timezone.utc))
        await storage.save_message(OWNER, "topic2", {"data": 2}, datetime.datetime.now(datetime.timezone.utc))

        await storage.clear()

        assert await storage.get_topic_length(OWNER, "topic1") == 0
        assert await storage.get_topic_length(OWNER, "topic2") == 0
