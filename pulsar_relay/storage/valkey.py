"""Valkey-based storage backend using Streams."""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional, Union, cast

from glide import (
    ExclusiveIdBound,
    GlideClient,
    GlideClientConfiguration,
    IdBound,
    InfoSection,
    MaxId,
    MinId,
    NodeAddress,
    ServerCredentials,
    TrimByMaxLen,
    TrimByMinId,
)

from pulsar_relay.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class UnsafeEvictionPolicyError(RuntimeError):
    """Valkey is configured with a maxmemory-policy that can evict relay state."""


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
        retention_seconds: int = 0,
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
            retention_seconds: Drop messages older than this many seconds.
                0 (the default) disables age-based retention, so streams are
                bounded only by ``max_messages_per_topic`` and the latest
                message of an idle topic stays readable indefinitely.
        """
        self.host = host
        self.port = port
        self.max_messages_per_topic = max_messages_per_topic
        self.retention_seconds = retention_seconds
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

    async def check_eviction_policy(self) -> None:
        """Check that Valkey will never evict relay state under memory pressure.

        Users, topics and access grants are stored without a TTL, and the
        JWT denylist, refresh tokens and device codes with one, so any
        eviction policy other than ``noeviction`` can delete them once
        ``maxmemory`` is reached (evicting a denylist entry re-enables a
        revoked token). With ``maxmemory 0`` nothing is ever evicted.

        If the configuration cannot be read, a warning is logged and the
        check passes.

        Raises:
            UnsafeEvictionPolicyError: If Valkey may evict keys.
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        try:
            info = await self._client.info([InfoSection.MEMORY])
        except Exception as e:
            logger.warning(f"Could not read Valkey memory info, unable to verify maxmemory-policy: {e}")
            return

        text = info.decode() if isinstance(info, bytes) else str(info)
        fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
        policy = fields.get("maxmemory_policy", "").strip()
        maxmemory = fields.get("maxmemory", "").strip()
        if not policy or not maxmemory.isdigit():
            logger.warning("Valkey did not report maxmemory/maxmemory_policy, unable to verify it is safe")
            return

        if int(maxmemory) > 0 and policy != "noeviction":
            raise UnsafeEvictionPolicyError(
                f"Valkey maxmemory-policy is '{policy}' with maxmemory {maxmemory}: once the limit is "
                "reached Valkey may evict users, topics, access grants and JWT denylist entries. "
                "Set 'maxmemory-policy noeviction' (or 'maxmemory 0')."
            )

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

    def _retention_cutoff_ms(self) -> int:
        """Oldest stream ID timestamp still within the retention window.

        Stream IDs are ``<milliseconds>-<sequence>``, so the cutoff is the
        current time minus ``retention_seconds``.
        """
        return max(int(time.time() * 1000) - self.retention_seconds * 1000, 0)

    @staticmethod
    def _parse_stream_id(stream_id: str) -> Optional[tuple[int, int]]:
        """Parse a stream ID into (milliseconds, sequence), or None if malformed."""
        ms, _, seq = stream_id.partition("-")
        try:
            return int(ms), int(seq or 0)
        except ValueError:
            return None

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

            # Trim stream to max length
            # Note: Using exact=True for predictable behavior. Approximate trimming
            # (exact=False) may not trim at all in some cases with Valkey GLIDE.
            trim = self._client.xtrim(
                stream_key,
                TrimByMaxLen(exact=True, threshold=self.max_messages_per_topic),
            )
            if self.retention_seconds:
                # Also drop entries older than the retention window, and expire
                # the whole stream once the topic has been idle for that long
                # (every entry would be stale by then).
                await asyncio.gather(
                    trim,
                    self._client.xtrim(
                        stream_key, TrimByMinId(exact=True, threshold=f"{self._retention_cutoff_ms()}-0")
                    ),
                    self._client.expire(stream_key, self.retention_seconds),
                )
            else:
                await trim

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
        # With retention enabled, entries past the window are trimmed on the
        # next write; skip them until then.
        cutoff_ms = self._retention_cutoff_ms() if self.retention_seconds else 0
        oldest: Union[MinId, IdBound] = IdBound(f"{cutoff_ms}-0") if cutoff_ms else MinId()
        start_bound: Union[MinId, IdBound, ExclusiveIdBound]

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

                start_bound = oldest
                stream_entries = await self._client.xrevrange(stream_key, end=end_bound, start=start_bound, count=limit)
            else:
                # Use XRANGE for forward order (oldest first)
                since_id = self._parse_stream_id(since) if since else None
                if since and (since_id is None or since_id >= (cutoff_ms, 0)):
                    start_bound = ExclusiveIdBound(since)
                else:
                    # No cursor, or cursor older than the retention window
                    start_bound = oldest
                end_bound = MaxId()
                stream_entries = await self._client.xrange(stream_key, start=start_bound, end=end_bound, count=limit)

            messages = []
            if stream_entries:
                # stream_entries is a Mapping[bytes, List[List[bytes]]]
                # Keys are stream IDs (bytes), values are list of [field, value] pairs
                for entry_id_bytes, field_value_list in stream_entries.items():
                    # Convert field-value pairs to dict
                    # Each pair is [field_name_bytes, field_value_bytes]
                    fields = {}
                    for pair in field_value_list:
                        field_name = pair[0].decode("utf-8")
                        field_value = pair[1].decode("utf-8")
                        fields[field_name] = field_value

                    # The stream ID IS the message ID
                    stream_id = entry_id_bytes.decode("utf-8")

                    # Parse the fields back into a message dict
                    message = {
                        "message_id": stream_id,  # Stream ID is now the message ID
                        "topic": topic,
                        "payload": json.loads(fields.get("payload", "{}")),
                        "timestamp": fields.get("timestamp", ""),
                        "stream_id": stream_id,  # Keep for backward compatibility
                    }

                    if "metadata" in fields:
                        message["metadata"] = json.loads(fields["metadata"])
                    else:
                        message["metadata"] = {}

                    messages.append(message)

            return messages

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
