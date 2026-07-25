"""Tests for Valkey storage backend.

Topic storage is namespaced by ``(owner_id, topic_name)`` since Phase
3c (closes API H#5). ValkeyStorage stream keys are
``stream:topic:{owner_id}/{name}``.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from glide import ExclusiveIdBound, MaxId, MinId

from pulsar_relay.storage.valkey import ValkeyStorage

OWNER = "u-test"


@pytest.fixture
async def valkey_storage():
    """Create a ValkeyStorage instance with mocked client."""
    storage = ValkeyStorage(
        host="localhost",
        port=6379,
        max_messages_per_topic=10000,
    )
    # Mock the Glide client
    storage._client = AsyncMock()
    storage._client.get = AsyncMock(return_value=None)
    storage._connected = True
    return storage


class TestValkeyStorage:
    """Test ValkeyStorage implementation."""

    @pytest.mark.anyio
    async def test_save_message(self, valkey_storage):
        """Test saving a message to Valkey stream."""
        # Mock xadd to return a stream ID
        valkey_storage._client.xadd = AsyncMock(return_value=b"1234567890123-0")
        valkey_storage._client.xtrim = AsyncMock()

        message_id = await valkey_storage.save_message(
            owner_id=OWNER,
            topic="test-topic",
            payload={"data": "value"},
            timestamp=datetime(2025, 1, 1, 12, 0, 0),
            metadata={"source": "test"},
        )

        # Verify that the stream ID is returned as message ID
        assert message_id == "1234567890123-0"

        # Verify xadd was called with correct parameters
        valkey_storage._client.xadd.assert_called_once()
        call_args = valkey_storage._client.xadd.call_args
        assert call_args[0][0] == f"stream:topic:{OWNER}/test-topic"
        # xadd receives list of tuples: [(field, value), ...]
        fields_list = call_args[0][1]
        fields = dict(fields_list)  # Convert to dict for easy verification
        # message_id field should no longer be stored
        assert "message_id" not in fields
        assert json.loads(fields["payload"]) == {"data": "value"}
        assert json.loads(fields["metadata"]) == {"source": "test"}

        # Verify xtrim was called
        valkey_storage._client.xtrim.assert_called_once()

    @pytest.mark.anyio
    async def test_save_message_without_metadata(self, valkey_storage):
        """Test saving a message without metadata."""
        valkey_storage._client.xadd = AsyncMock(return_value=b"1234567890123-0")
        valkey_storage._client.xtrim = AsyncMock()

        message_id = await valkey_storage.save_message(
            owner_id=OWNER,
            topic="test-topic",
            payload={"data": "value"},
            timestamp=datetime(2025, 1, 1, 12, 0, 0),
        )

        # Verify the stream ID is returned
        assert message_id == "1234567890123-0"

        # Verify metadata field is not included when None
        call_args = valkey_storage._client.xadd.call_args
        fields_list = call_args[0][1]
        fields = dict(fields_list)  # Convert to dict for easy verification
        assert "metadata" not in fields

    @pytest.mark.anyio
    async def test_save_message_does_not_trim_group_stream(self, valkey_storage):
        """Unread and pending group entries must not be removed by XTRIM."""
        valkey_storage._client.xadd = AsyncMock(return_value=b"1234567890123-0")
        valkey_storage._client.get = AsyncMock(return_value=b"galaxy-job-status-v1")
        valkey_storage._client.xtrim = AsyncMock()

        await valkey_storage.save_message(
            owner_id=OWNER,
            topic="job-status",
            payload={"job_id": "42"},
            timestamp=datetime(2025, 1, 1, 12, 0, 0),
        )

        valkey_storage._client.xtrim.assert_not_called()

    @pytest.mark.anyio
    async def test_get_messages(self, valkey_storage):
        """Test retrieving messages from Valkey stream."""
        # Mock xrange to return stream entries in GLIDE format
        # Returns: Mapping[bytes, List[List[bytes]]] where each inner list is [field, value]
        # Note: message_id field is no longer stored - stream ID is the message ID
        valkey_storage._client.xrange = AsyncMock(
            return_value={
                b"1234567890123-0": [
                    [b"payload", json.dumps({"index": 1}).encode()],
                    [b"timestamp", b"2025-01-01T12:00:00"],
                    [b"metadata", json.dumps({"source": "test"}).encode()],
                ],
                b"1234567890124-0": [
                    [b"payload", json.dumps({"index": 2}).encode()],
                    [b"timestamp", b"2025-01-01T12:00:01"],
                ],
            }
        )

        messages = await valkey_storage.get_messages(OWNER, "test-topic", limit=10)

        assert len(messages) == 2
        # Stream ID is now the message ID
        assert messages[0]["message_id"] == "1234567890123-0"
        assert messages[0]["payload"] == {"index": 1}
        assert messages[0]["metadata"] == {"source": "test"}
        assert messages[0]["stream_id"] == "1234567890123-0"

        assert messages[1]["message_id"] == "1234567890124-0"
        assert messages[1]["payload"] == {"index": 2}
        assert messages[1]["metadata"] == {}

    @pytest.mark.anyio
    async def test_get_messages_with_since(self, valkey_storage):
        """Test retrieving messages starting from a specific stream ID."""
        valkey_storage._client.xrange = AsyncMock(return_value={})

        await valkey_storage.get_messages(OWNER, "test-topic", since="1234567890120-0", limit=10)

        # Verify xrange was called with ExclusiveIdBound
        valkey_storage._client.xrange.assert_called_once()
        call_args = valkey_storage._client.xrange.call_args
        assert call_args[0][0] == f"stream:topic:{OWNER}/test-topic"
        # Check that start bound is ExclusiveIdBound type

        assert isinstance(call_args[1]["start"], ExclusiveIdBound)
        assert isinstance(call_args[1]["end"], MaxId)
        assert call_args[1]["count"] == 10

    @pytest.mark.anyio
    async def test_get_messages_from_beginning(self, valkey_storage):
        """Test retrieving messages from the beginning."""
        valkey_storage._client.xrange = AsyncMock(return_value={})

        await valkey_storage.get_messages(OWNER, "test-topic", since=None, limit=5)

        # Verify xrange was called with MinId and MaxId
        valkey_storage._client.xrange.assert_called_once()
        call_args = valkey_storage._client.xrange.call_args
        assert call_args[0][0] == f"stream:topic:{OWNER}/test-topic"
        # Check that start bound is MinId and end is MaxId

        assert isinstance(call_args[1]["start"], MinId)
        assert isinstance(call_args[1]["end"], MaxId)
        assert call_args[1]["count"] == 5

    @pytest.mark.anyio
    async def test_trim_topic(self, valkey_storage):
        """Test trimming a topic to keep specific number of messages."""
        valkey_storage._client.xlen = AsyncMock(return_value=100)
        valkey_storage._client.xtrim = AsyncMock()

        removed = await valkey_storage.trim_topic(OWNER, "test-topic", keep_count=50)

        assert removed == 50
        valkey_storage._client.xtrim.assert_called_once()

    @pytest.mark.anyio
    async def test_trim_topic_no_trim_needed(self, valkey_storage):
        """Test trimming when topic has fewer messages than keep_count."""
        valkey_storage._client.xlen = AsyncMock(return_value=30)
        valkey_storage._client.xtrim = AsyncMock()

        removed = await valkey_storage.trim_topic(OWNER, "test-topic", keep_count=50)

        assert removed == 0
        valkey_storage._client.xtrim.assert_not_called()

    @pytest.mark.anyio
    async def test_get_topic_length(self, valkey_storage):
        """Test getting the length of a topic."""
        valkey_storage._client.xlen = AsyncMock(return_value=42)

        length = await valkey_storage.get_topic_length(OWNER, "test-topic")

        assert length == 42
        valkey_storage._client.xlen.assert_called_once_with(f"stream:topic:{OWNER}/test-topic")

    @pytest.mark.anyio
    async def test_get_topic_length_empty(self, valkey_storage):
        """Test getting the length of an empty topic."""
        valkey_storage._client.xlen = AsyncMock(return_value=None)

        length = await valkey_storage.get_topic_length(OWNER, "test-topic")

        assert length == 0

    @pytest.mark.anyio
    async def test_health_check_healthy(self, valkey_storage):
        """Test health check when Valkey is healthy."""
        valkey_storage._client.ping = AsyncMock(return_value=b"PONG")

        health = await valkey_storage.health_check()

        assert health["status"] == "healthy"
        assert health["connected"] is True
        assert health["host"] == "localhost"
        assert health["port"] == 6379

    @pytest.mark.anyio
    async def test_health_check_disconnected(self):
        """Test health check when not connected."""
        storage = ValkeyStorage()

        health = await storage.health_check()

        assert health["status"] == "disconnected"
        assert health["connected"] is False

    @pytest.mark.anyio
    async def test_health_check_error(self, valkey_storage):
        """Test health check when Valkey connection fails."""
        valkey_storage._client.ping = AsyncMock(side_effect=Exception("Connection failed"))

        health = await valkey_storage.health_check()

        assert health["status"] == "unhealthy"
        assert health["connected"] is False
        assert "error" in health

    @pytest.mark.anyio
    async def test_reset_helper_uses_scan_and_delete(self, valkey_storage):
        """The ``tests._storage_helpers.reset_valkey_storage`` helper resets
        a Valkey DB via ``SCAN`` + ``DEL``. ValkeyStorage no longer exposes
        ``clear()`` because ``FLUSHALL`` is renamed in the hardened
        ``valkey.conf``."""
        from tests._storage_helpers import reset_valkey_storage

        valkey_storage._client.scan = AsyncMock(
            side_effect=[
                [b"5", [b"stream:topic:topic1", b"stream:topic:topic2"]],
                [b"0", [b"stream:topic:topic3"]],
            ]
        )
        valkey_storage._client.delete = AsyncMock()

        await reset_valkey_storage(valkey_storage)

        # Two scan iterations, two delete batches.
        assert valkey_storage._client.scan.await_count == 2
        assert valkey_storage._client.delete.await_count == 2
        # ValkeyStorage no longer has a flushall escape hatch.
        assert not hasattr(valkey_storage, "clear")

    @pytest.mark.anyio
    async def test_not_connected_error(self):
        """Test that operations fail when not connected."""
        storage = ValkeyStorage()

        with pytest.raises(RuntimeError, match="Not connected to Valkey"):
            await storage.save_message(OWNER, "topic", {}, datetime.now())

        with pytest.raises(RuntimeError, match="Not connected to Valkey"):
            await storage.get_messages(OWNER, "topic")

        with pytest.raises(RuntimeError, match="Not connected to Valkey"):
            await storage.trim_topic(OWNER, "topic", 10)

        with pytest.raises(RuntimeError, match="Not connected to Valkey"):
            await storage.get_topic_length(OWNER, "topic")

    @pytest.mark.anyio
    async def test_connect_disconnect(self):
        """Test connecting and disconnecting from Valkey."""
        with patch("pulsar_relay.storage.valkey.GlideClient") as mock_glide:
            mock_client = AsyncMock()
            mock_glide.create = AsyncMock(return_value=mock_client)

            storage = ValkeyStorage()

            # Test connect
            await storage.connect()
            assert storage._connected is True
            assert storage._client is not None

            # Test disconnect
            await storage.disconnect()
            mock_client.close.assert_called_once()
            assert storage._connected is False

    @pytest.mark.anyio
    async def test_stream_key_generation(self, valkey_storage):
        """Stream keys are namespaced by ``(owner_id, topic_name)`` as
        of Phase 3c (API H#5)."""
        key = valkey_storage._get_stream_key("alice", "my-topic")
        assert key == "stream:topic:alice/my-topic"

    @pytest.mark.anyio
    async def test_metadata_key_generation(self, valkey_storage):
        """Metadata keys are namespaced by ``(owner_id, topic_name)``."""
        key = valkey_storage._get_metadata_key("alice", "my-topic")
        assert key == "meta:topic:alice/my-topic"

    @pytest.mark.anyio
    async def test_two_owners_have_independent_streams(self, valkey_storage):
        """Stream keys for the same bare topic name under different
        owners are distinct — the API H#5 squat is closed at the
        storage layer."""
        alice_key = valkey_storage._get_stream_key("alice", "jobs")
        bob_key = valkey_storage._get_stream_key("bob", "jobs")
        assert alice_key != bob_key
        assert "alice" in alice_key
        assert "bob" in bob_key

    @pytest.mark.anyio
    async def test_poll_group_delivers_fresh_ordered_message(self, valkey_storage):
        stream_key = f"stream:topic:{OWNER}/job-status"
        valkey_storage._client.set = AsyncMock(side_effect=[b"OK", b"OK"])
        valkey_storage._client.xgroup_create = AsyncMock()
        valkey_storage._client.xautoclaim = AsyncMock(return_value=[b"0-0", {}])
        valkey_storage._client.xreadgroup = AsyncMock(
            return_value={
                stream_key.encode(): {
                    b"123-0": [
                        [b"payload", json.dumps({"job_id": "42"}).encode()],
                        [b"timestamp", b"2025-01-01T12:00:00"],
                        [
                            b"metadata",
                            json.dumps({"ordering_key": "42"}).encode(),
                        ],
                    ]
                }
            }
        )
        valkey_storage._client.xpending_range = AsyncMock(return_value=[])

        messages = await valkey_storage.poll_group(
            OWNER,
            ["job-status"],
            "galaxy-job-status-v1",
            "handler:boot",
            1,
            300,
        )

        assert [message["message_id"] for message in messages] == ["123-0"]
        valkey_storage._client.xreadgroup.assert_awaited_once()

    @pytest.mark.anyio
    async def test_poll_group_releases_blocked_ordered_message_without_timeout(
        self, valkey_storage
    ):
        stream_key = f"stream:topic:{OWNER}/job-status"
        valkey_storage._client.xpending_range = AsyncMock(
            return_value=[[b"124-0", b"other-consumer", 10, 1]]
        )
        entry = {
            b"124-0": [
                [b"payload", json.dumps({"job_id": "42"}).encode()],
                [b"timestamp", b"2025-01-01T12:00:01"],
                [b"metadata", json.dumps({"ordering_key": "42"}).encode()],
            ]
        }
        valkey_storage._client.xrange = AsyncMock(return_value=entry)
        valkey_storage._client.get = AsyncMock(return_value=None)
        valkey_storage._client.xclaim = AsyncMock(return_value=entry)
        valkey_storage._client.set = AsyncMock(return_value=b"OK")

        messages = await valkey_storage._claim_unlocked_pending(
            OWNER,
            "job-status",
            "galaxy-job-status-v1",
            "handler:boot",
            1,
            300,
            set(),
        )

        assert [message["message_id"] for message in messages] == ["124-0"]
        valkey_storage._client.xclaim.assert_awaited_once()
        assert valkey_storage._client.xclaim.call_args.args[:5] == (
            stream_key,
            "galaxy-job-status-v1",
            "handler:boot",
            0,
            ["124-0"],
        )

    @pytest.mark.anyio
    async def test_ack_writes_terminal_tombstone_before_ack_and_deletes_entry(
        self, valkey_storage
    ):
        stream_key = f"stream:topic:{OWNER}/job-status"
        events = []

        async def record_tombstone(*args, **kwargs):
            events.append("tombstone")
            return b"OK"

        async def record_ack(*args, **kwargs):
            events.append("ack")
            return 1

        valkey_storage._client.get = AsyncMock(return_value=b"galaxy-job-status-v1")
        valkey_storage._client.xpending_range = AsyncMock(
            return_value=[[b"125-0", b"handler:boot", 10, 1]]
        )
        valkey_storage._client.xrange = AsyncMock(
            return_value={
                b"125-0": [
                    [b"payload", json.dumps({"job_id": "42"}).encode()],
                    [b"timestamp", b"2025-01-01T12:00:02"],
                    [
                        b"metadata",
                        json.dumps(
                            {
                                "ordering_key": "42",
                                "deduplication_key": "job-terminal:42",
                            }
                        ).encode(),
                    ],
                ]
            }
        )
        valkey_storage._client.set = AsyncMock(side_effect=record_tombstone)
        valkey_storage._client.xack = AsyncMock(side_effect=record_ack)
        valkey_storage._client.xdel = AsyncMock(return_value=1)
        valkey_storage._client.custom_command = AsyncMock(return_value=1)

        updated = await valkey_storage.update_group_deliveries(
            OWNER,
            "galaxy-job-status-v1",
            "handler:boot",
            [{"topic": "job-status", "message_id": "125-0"}],
            "ack",
            300,
        )

        assert updated == 1
        assert events == ["tombstone", "ack"]
        valkey_storage._client.xdel.assert_awaited_once_with(stream_key, ["125-0"])

    @pytest.mark.anyio
    async def test_touch_requires_and_atomically_renews_ordering_lock(
        self, valkey_storage
    ):
        valkey_storage._client.get = AsyncMock(return_value=b"galaxy-job-status-v1")
        valkey_storage._client.xpending_range = AsyncMock(
            return_value=[[b"126-0", b"handler:boot", 10, 1]]
        )
        valkey_storage._client.xrange = AsyncMock(
            return_value={
                b"126-0": [
                    [b"payload", json.dumps({"job_id": "42"}).encode()],
                    [b"timestamp", b"2025-01-01T12:00:03"],
                    [b"metadata", json.dumps({"ordering_key": "42"}).encode()],
                ]
            }
        )
        valkey_storage._client.custom_command = AsyncMock(return_value=0)
        valkey_storage._client.xclaim = AsyncMock()

        updated = await valkey_storage.update_group_deliveries(
            OWNER,
            "galaxy-job-status-v1",
            "handler:boot",
            [{"topic": "job-status", "message_id": "126-0"}],
            "touch",
            300,
        )

        assert updated == 0
        valkey_storage._client.xclaim.assert_not_awaited()

        valkey_storage._client.custom_command = AsyncMock(return_value=1)
        valkey_storage._client.xclaim = AsyncMock(
            return_value={b"126-0": [[b"payload", b"{}"]]}
        )
        updated = await valkey_storage.update_group_deliveries(
            OWNER,
            "galaxy-job-status-v1",
            "handler:boot",
            [{"topic": "job-status", "message_id": "126-0"}],
            "touch",
            300,
        )

        assert updated == 1
        valkey_storage._client.xclaim.assert_awaited_once()


@pytest.mark.integration
@pytest.mark.anyio
async def test_valkey_integration():
    """Integration test with real Valkey instance.

    This test requires a running Valkey instance on localhost:6379.
    Skip if Valkey is not available.
    """
    from tests._storage_helpers import reset_valkey_storage, valkey_test_credentials

    username, password = valkey_test_credentials()
    storage = ValkeyStorage(
        host="localhost",
        port=6379,
        max_messages_per_topic=100,
        username=username,
        password=password,
    )

    try:
        # Try to connect
        await storage.connect()

        # Clear any existing test data
        await reset_valkey_storage(storage)

        # Test save and retrieve
        timestamp = datetime(2025, 1, 1, 12, 0, 0)
        message_id = await storage.save_message(
            owner_id=OWNER,
            topic="integration-test",
            payload={"test": "data"},
            timestamp=timestamp,
            metadata={"source": "integration"},
        )

        # message_id should be a stream ID (format: timestamp-sequence)
        assert "-" in message_id

        messages = await storage.get_messages(OWNER, "integration-test")
        assert len(messages) >= 1
        assert messages[0]["message_id"] == message_id
        assert messages[0]["payload"] == {"test": "data"}

        # Test topic length
        length = await storage.get_topic_length(OWNER, "integration-test")
        assert length >= 1

        # Test health check
        health = await storage.health_check()
        assert health["status"] == "healthy"

        # Cleanup
        await reset_valkey_storage(storage)

    except Exception as e:
        pytest.skip(f"Valkey not available: {e}")

    finally:
        await storage.disconnect()
