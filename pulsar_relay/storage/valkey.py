"""Valkey-based storage backend using Streams."""

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional, Union, cast

from glide import (
    ConditionalChange,
    ExclusiveIdBound,
    ExpirySet,
    ExpiryType,
    GlideClient,
    GlideClientConfiguration,
    IdBound,
    MaxId,
    MinId,
    NodeAddress,
    ServerCredentials,
    StreamClaimOptions,
    StreamGroupOptions,
    StreamPendingOptions,
    StreamReadGroupOptions,
    TrimByMaxLen,
)

from pulsar_relay.storage.base import ConsumerGroupConflictError, StorageBackend

logger = logging.getLogger(__name__)


class ValkeyStorage(StorageBackend):
    """Storage backend using Valkey Streams for message persistence.

    Uses Valkey Streams (XADD, XREAD, XTRIM) for efficient message storage
    and retrieval with automatic trimming based on retention policies.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        max_messages_per_topic: int = 1000000,
        use_tls: bool = False,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        """Initialize Valkey storage backend.

        Args:
            host: Valkey host
            port: Valkey port
            max_messages_per_topic: Maximum messages per topic before trimming
            use_tls: Whether to use TLS for connection
            username: Valkey ACL username (None falls back to legacy
                requirepass authentication when ``password`` is supplied).
            password: Valkey password / ACL password. None disables AUTH —
                only acceptable in test/dev configurations.

        Note:
            The previous ``ttl_seconds`` parameter was accepted but
            never enforced (the stream keys never received an
            ``EXPIRE``). It has been removed to avoid implying a
            retention guarantee that did not exist. Retention is
            bounded only by ``max_messages_per_topic`` via stream
            trim. Closes Storage H#6.
        """
        self.host = host
        self.port = port
        self.max_messages_per_topic = max_messages_per_topic
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self._client: Optional[GlideClient] = None
        self._connected = False

    async def connect(self) -> None:
        """Connect to Valkey server."""
        if self._connected:
            return

        try:
            credentials: Optional[ServerCredentials] = None
            if self.password is not None:
                # Valkey 9 rejects ``HELLO ... AUTH "" <pw>`` (which is
                # what ``glide_shared`` emits when ``username`` is falsy)
                # with WRONGPASS, even though ``redis-cli -a`` (legacy
                # ``AUTH <pw>``) works against the same instance. When
                # the operator has only configured a password (legacy
                # ``--requirepass`` mode), pin the username to
                # ``"default"`` — the implicit ACL user that
                # ``--requirepass`` configures.
                credentials = ServerCredentials(
                    username=self.username or "default",
                    password=self.password,
                )
            config = GlideClientConfiguration(
                addresses=[NodeAddress(host=self.host, port=self.port)],
                use_tls=self.use_tls,
                request_timeout=5000,  # 5 second timeout
                credentials=credentials,
            )
            self._client = await GlideClient.create(config)
            self._connected = True
            logger.info(f"Connected to Valkey at {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to connect to Valkey: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Valkey server."""
        if self._client:
            await self._client.close()
            self._connected = False
            logger.info("Disconnected from Valkey")

    def _get_stream_key(self, owner_id: str, topic: str) -> str:
        """Stream key namespaced by topic owner (API H#5).

        Returns ``stream:topic:{owner_id}/{topic}``. Two users with the
        same bare topic name have entirely distinct streams.
        """
        return f"stream:topic:{owner_id}/{topic}"

    def _get_metadata_key(self, owner_id: str, topic: str) -> str:
        """Metadata key namespaced by topic owner.

        Returns ``meta:topic:{owner_id}/{topic}``.
        """
        return f"meta:topic:{owner_id}/{topic}"

    def _get_group_binding_key(self, owner_id: str, topic: str) -> str:
        return f"group:topic:{owner_id}/{topic}"

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _get_ordering_lock_key(self, owner_id: str, topic: str, group: str, ordering_key: str) -> str:
        return f"group:lock:{owner_id}/{topic}:{self._digest(group)}:{self._digest(ordering_key)}"

    def _get_dedupe_key(self, owner_id: str, topic: str, group: str, dedupe_key: str) -> str:
        return f"group:dedupe:{owner_id}/{topic}:{self._digest(group)}:{self._digest(dedupe_key)}"

    @staticmethod
    def _decode_entries(topic: str, stream_entries: Any) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if not stream_entries:
            return messages
        for entry_id_bytes, field_value_list in stream_entries.items():
            fields = {pair[0].decode("utf-8"): pair[1].decode("utf-8") for pair in field_value_list}
            stream_id = entry_id_bytes.decode("utf-8")
            messages.append(
                {
                    "message_id": stream_id,
                    "topic": topic,
                    "payload": json.loads(fields.get("payload", "{}")),
                    "timestamp": fields.get("timestamp", ""),
                    "stream_id": stream_id,
                    "metadata": json.loads(fields.get("metadata", "{}")),
                }
            )
        return messages

    async def save_message(
        self,
        owner_id: str,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Save a message to Valkey Stream.

        Args:
            topic: Topic name
            payload: Message payload
            timestamp: Message timestamp
            metadata: Optional message metadata

        Returns:
            The stream ID assigned by Valkey (e.g., "1234567890123-0")
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(owner_id, topic)

        # Prepare stream entry as list of tuples (GLIDE API requirement)
        # Valkey Streams stores fields as key-value pairs
        # Note: We no longer store a separate message_id field - the stream ID IS the message ID
        fields: list[tuple[Union[str, bytes], Union[str, bytes]]] = [
            ("payload", json.dumps(payload)),
            ("timestamp", timestamp.isoformat()),
        ]

        if metadata:
            fields.append(("metadata", json.dumps(metadata)))

        try:
            # Add message to stream with auto-generated ID
            # XADD returns the stream entry ID (e.g., b"1234567890123-0")
            stream_entry_id = await self._client.xadd(stream_key, fields)

            if not stream_entry_id:
                raise RuntimeError("Failed to add message to stream - no ID returned")

            # Decode the stream ID to return as message ID
            message_id = cast(str, stream_entry_id.decode("utf-8"))

            # Consumer-group streams retain undispatched and pending messages.
            # Cursor/broadcast topics retain the existing bounded behavior.
            if await self._client.get(self._get_group_binding_key(owner_id, topic)) is None:
                await self._client.xtrim(
                    stream_key,
                    TrimByMaxLen(exact=True, threshold=self.max_messages_per_topic),
                )

            logger.debug(f"Saved message to topic {topic} with stream ID {message_id}")

            return message_id

        except Exception as e:
            logger.error(f"Failed to save message to Valkey: {e}")
            raise

    async def get_messages(
        self,
        owner_id: str,
        topic: str,
        since: Optional[str] = None,
        limit: int = 10,
        reverse: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve messages from ``owner_id``'s Valkey Stream."""
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(owner_id, topic)

        try:
            if reverse:
                # Use XREVRANGE for reverse order (newest first)
                # XREVRANGE signature: xrevrange(key, end, start, count)
                # Parameters: end=highest ID, start=lowest ID
                # When since is provided, we want messages BEFORE (older than) that ID
                if since:
                    # End at just before the 'since' ID
                    end_bound = ExclusiveIdBound(since)
                else:
                    # End at the most recent message
                    end_bound = MaxId()

                start_bound = MinId()  # Start from the beginning
                stream_entries = await self._client.xrevrange(stream_key, end=end_bound, start=start_bound, count=limit)
            else:
                # Use XRANGE for forward order (oldest first)
                start_bound = ExclusiveIdBound(since) if since else MinId()
                end_bound = MaxId()
                stream_entries = await self._client.xrange(stream_key, start=start_bound, end=end_bound, count=limit)

            return self._decode_entries(topic, stream_entries)

        except Exception as e:
            logger.error(f"Failed to get messages from Valkey: {e}")
            raise

    async def trim_topic(self, owner_id: str, topic: str, keep_count: int) -> int:
        """Trim a topic to keep only the most recent messages.

        Args:
            owner_id: Topic owner.
            topic: Topic name (bare; namespacing applied internally).
            keep_count: Number of messages to keep

        Returns:
            Number of messages removed
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(owner_id, topic)

        try:
            # Get current length
            info = await self._client.xlen(stream_key)
            current_length = info if info else 0

            if current_length <= keep_count:
                return 0

            # Trim to keep_count messages
            await self._client.xtrim(stream_key, TrimByMaxLen(exact=True, threshold=keep_count))

            # Return number of messages removed
            removed = current_length - keep_count
            logger.info(f"Trimmed topic {topic}: removed {removed} messages")
            return removed

        except Exception as e:
            logger.error(f"Failed to trim topic in Valkey: {e}")
            raise

    async def get_topic_length(self, owner_id: str, topic: str) -> int:
        """Get the number of messages in a topic.

        Args:
            owner_id: Topic owner.
            topic: Topic name (bare).

        Returns:
            Number of messages in the topic
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(owner_id, topic)

        try:
            length = await self._client.xlen(stream_key)
            return length if length else 0
        except Exception as e:
            logger.error(f"Failed to get topic length from Valkey: {e}")
            raise

    async def get_consumer_group(self, owner_id: str, topic: str) -> Optional[str]:
        if not self._client:
            raise RuntimeError("Not connected to Valkey")
        value = await self._client.get(self._get_group_binding_key(owner_id, topic))
        return value.decode("utf-8") if value else None

    async def _ensure_consumer_group(self, owner_id: str, topic: str, group: str) -> None:
        assert self._client is not None
        binding_key = self._get_group_binding_key(owner_id, topic)
        claimed = await self._client.set(
            binding_key,
            group,
            conditional_set=ConditionalChange.ONLY_IF_DOES_NOT_EXIST,
        )
        bound_group = group if claimed else await self.get_consumer_group(owner_id, topic)
        if bound_group != group:
            raise ConsumerGroupConflictError(f"Topic {topic!r} is already assigned to consumer group {bound_group!r}")
        try:
            # "$" deliberately starts at the current tail. Galaxy performs a
            # status sweep during the coordinated cutover.
            await self._client.xgroup_create(
                self._get_stream_key(owner_id, topic),
                group,
                "$",
                options=StreamGroupOptions(make_stream=True),
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _claim_ordering_key(
        self,
        owner_id: str,
        topic: str,
        group: str,
        message: dict[str, Any],
        visibility_timeout: int,
    ) -> bool:
        assert self._client is not None
        metadata = message.get("metadata") or {}
        dedupe_key = metadata.get("deduplication_key")
        if dedupe_key and await self._client.get(self._get_dedupe_key(owner_id, topic, group, dedupe_key)):
            stream_key = self._get_stream_key(owner_id, topic)
            await self._client.xack(stream_key, group, [message["message_id"]])
            await self._client.xdel(stream_key, [message["message_id"]])
            return False
        ordering_key = metadata.get("ordering_key")
        if not ordering_key:
            return True
        lock_key = self._get_ordering_lock_key(owner_id, topic, group, ordering_key)
        expiry = ExpirySet(ExpiryType.MILLSEC, visibility_timeout * 1000)
        current = await self._client.get(lock_key)
        if current and current.decode("utf-8") == message["message_id"]:
            renewed = await self._client.custom_command(
                [
                    "EVAL",
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end",
                    "1",
                    lock_key,
                    message["message_id"],
                    str(visibility_timeout * 1000),
                ]
            )
            return renewed in (1, b"1")
        claimed = await self._client.set(
            lock_key,
            message["message_id"],
            conditional_set=ConditionalChange.ONLY_IF_DOES_NOT_EXIST,
            expiry=expiry,
        )
        return claimed is not None

    async def _claim_unlocked_pending(
        self,
        owner_id: str,
        topic: str,
        group: str,
        consumer: str,
        limit: int,
        visibility_timeout: int,
        excluded_message_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Claim ordered entries which were blocked behind an earlier entry.

        XREADGROUP makes a fresh entry pending before Relay can inspect its
        ordering key. If another entry for that key owns the lock, the new
        entry remains pending. Once the earlier entry is acknowledged and
        releases the lock, this scan makes the blocked entry immediately
        available instead of waiting for the full visibility timeout.
        """
        assert self._client is not None
        stream_key = self._get_stream_key(owner_id, topic)
        pending = await self._client.xpending_range(
            stream_key,
            group,
            MinId(),
            MaxId(),
            max(limit * 10, 100),
        )
        claimed: list[dict[str, Any]] = []
        for pending_entry in pending:
            if len(claimed) >= limit:
                break
            raw_message_id = pending_entry[0]
            message_id = raw_message_id.decode("utf-8") if isinstance(raw_message_id, bytes) else str(raw_message_id)
            if message_id in excluded_message_ids:
                continue
            entry = await self._client.xrange(stream_key, IdBound(message_id), IdBound(message_id), count=1)
            decoded = self._decode_entries(topic, entry)
            if not decoded:
                continue
            message = decoded[0]
            metadata = message.get("metadata") or {}
            ordering_key = metadata.get("ordering_key")
            if not ordering_key:
                continue
            lock_key = self._get_ordering_lock_key(owner_id, topic, group, ordering_key)
            if await self._client.get(lock_key):
                continue
            transferred = await self._client.xclaim(
                stream_key,
                group,
                consumer,
                0,
                [message_id],
                options=StreamClaimOptions(idle=0),
            )
            transferred_messages = self._decode_entries(topic, transferred)
            if not transferred_messages:
                continue
            transferred_message = transferred_messages[0]
            if await self._claim_ordering_key(owner_id, topic, group, transferred_message, visibility_timeout):
                claimed.append(transferred_message)
        return claimed

    async def poll_group(
        self,
        owner_id: str,
        topics: list[str],
        group: str,
        consumer: str,
        limit: int,
        visibility_timeout: int,
    ) -> list[dict[str, Any]]:
        if not self._client:
            raise RuntimeError("Not connected to Valkey")
        messages: list[dict[str, Any]] = []
        for topic in topics:
            await self._ensure_consumer_group(owner_id, topic, group)
            remaining = limit - len(messages)
            if remaining <= 0:
                break
            stream_key = self._get_stream_key(owner_id, topic)

            reclaimed = await self._client.xautoclaim(
                stream_key,
                group,
                consumer,
                visibility_timeout * 1000,
                "0-0",
                count=remaining,
            )
            reclaimed_entries = reclaimed[1] if len(reclaimed) > 1 and isinstance(reclaimed[1], Mapping) else {}
            candidates = self._decode_entries(topic, reclaimed_entries)

            remaining -= len(candidates)
            if remaining > 0:
                fresh = await self._client.xreadgroup(
                    {stream_key: ">"},
                    group,
                    consumer,
                    options=StreamReadGroupOptions(count=remaining),
                )
                if fresh:
                    stream_entries = fresh.get(stream_key.encode("utf-8"))
                    candidates.extend(self._decode_entries(topic, stream_entries))

            candidate_ids = {message["message_id"] for message in candidates}
            for message in candidates:
                if await self._claim_ordering_key(owner_id, topic, group, message, visibility_timeout):
                    messages.append(message)
                    if len(messages) >= limit:
                        break
            if len(messages) < limit:
                messages.extend(
                    await self._claim_unlocked_pending(
                        owner_id,
                        topic,
                        group,
                        consumer,
                        limit - len(messages),
                        visibility_timeout,
                        candidate_ids,
                    )
                )
        return messages

    async def _delivery_owned_by(self, stream_key: str, group: str, consumer: str, message_id: str) -> bool:
        assert self._client is not None
        pending = await self._client.xpending_range(
            stream_key,
            group,
            IdBound(message_id),
            IdBound(message_id),
            1,
            options=StreamPendingOptions(consumer_name=consumer),
        )
        if not pending:
            return False
        raw_message_id = pending[0][0]
        pending_message_id = (
            raw_message_id.decode("utf-8") if isinstance(raw_message_id, bytes) else str(raw_message_id)
        )
        return pending_message_id == message_id

    async def update_group_deliveries(
        self,
        owner_id: str,
        group: str,
        consumer: str,
        deliveries: list[dict[str, str]],
        action: str,
        visibility_timeout: int,
    ) -> int:
        if not self._client:
            raise RuntimeError("Not connected to Valkey")
        updated = 0
        for delivery in deliveries:
            topic = delivery["topic"]
            message_id = delivery["message_id"]
            if await self.get_consumer_group(owner_id, topic) != group:
                raise ConsumerGroupConflictError(f"Topic {topic!r} is not assigned to group {group!r}")
            stream_key = self._get_stream_key(owner_id, topic)
            if not await self._delivery_owned_by(stream_key, group, consumer, message_id):
                continue
            entry = await self._client.xrange(stream_key, IdBound(message_id), IdBound(message_id), count=1)
            decoded = self._decode_entries(topic, entry)
            if not decoded:
                continue
            message = decoded[0]
            metadata = message.get("metadata") or {}
            ordering_key = metadata.get("ordering_key")
            lock_key = self._get_ordering_lock_key(owner_id, topic, group, ordering_key) if ordering_key else None
            if action == "touch":
                if lock_key:
                    renewed = await self._client.custom_command(
                        [
                            "EVAL",
                            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                            "return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end",
                            "1",
                            lock_key,
                            message_id,
                            str(visibility_timeout * 1000),
                        ]
                    )
                    if renewed not in (1, b"1"):
                        continue
                touched = await self._client.xclaim(
                    stream_key,
                    group,
                    consumer,
                    0,
                    [message_id],
                    options=StreamClaimOptions(idle=0),
                )
                if not touched:
                    continue
            else:
                dedupe_key = metadata.get("deduplication_key")
                if dedupe_key:
                    # Write the tombstone before ACK. A process failure between
                    # these operations can only suppress an equivalent terminal
                    # duplicate; doing them in the opposite order could expose
                    # such a duplicate for processing.
                    await self._client.set(
                        self._get_dedupe_key(owner_id, topic, group, dedupe_key),
                        "1",
                        expiry=ExpirySet(ExpiryType.SEC, 7 * 24 * 60 * 60),
                    )
                if await self._client.xack(stream_key, group, [message_id]) != 1:
                    continue
                await self._client.xdel(stream_key, [message_id])
                if lock_key:
                    await self._client.custom_command(
                        [
                            "EVAL",
                            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                            "return redis.call('DEL', KEYS[1]) else return 0 end",
                            "1",
                            lock_key,
                            message_id,
                        ]
                    )
            updated += 1
        return updated

    async def health_check(self) -> dict[str, Any]:
        """Check Valkey connection health.

        Returns:
            Dictionary with health status information
        """
        if not self._client:
            return {"status": "disconnected", "connected": False}

        try:
            # PING command to check connectivity
            pong = await self._client.ping()

            # Check if PONG response is healthy (can be bytes or string)
            is_healthy = pong == b"PONG" or pong == "PONG"

            return {
                "status": "healthy" if is_healthy else "unhealthy",
                "connected": self._connected,
                "host": self.host,
                "port": self.port,
            }
        except Exception as e:
            logger.error(f"Valkey health check failed: {e}")
            return {
                "status": "unhealthy",
                "connected": False,
                "error": str(e),
            }

    async def close(self) -> None:
        """Close the Valkey connection."""
        await self.disconnect()
