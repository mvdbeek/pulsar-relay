"""Valkey-based storage backend using Streams."""

import json
import logging
from datetime import datetime
from typing import Any, Optional, Union

from glide import GlideClient, GlideClientConfiguration, NodeAddress
from glide.async_commands.stream import ExclusiveIdBound, MaxId, MinId, TrimByMaxLen

from app.storage.base import StorageBackend

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
        ttl_seconds: int = 3600,
        use_tls: bool = False,
    ):
        """Initialize Valkey storage backend.

        Args:
            host: Valkey host
            port: Valkey port
            max_messages_per_topic: Maximum messages per topic before trimming
            ttl_seconds: Time-to-live for messages in seconds
            use_tls: Whether to use TLS for connection
        """
        self.host = host
        self.port = port
        self.max_messages_per_topic = max_messages_per_topic
        self.ttl_seconds = ttl_seconds
        self.use_tls = use_tls
        self._client: Optional[GlideClient] = None
        self._connected = False

    async def connect(self) -> None:
        """Connect to Valkey server."""
        if self._connected:
            return

        try:
            config = GlideClientConfiguration(
                addresses=[NodeAddress(host=self.host, port=self.port)],
                use_tls=self.use_tls,
                request_timeout=5000,  # 5 second timeout
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

    def _get_stream_key(self, topic: str) -> str:
        """Get the Valkey stream key for a topic.

        Args:
            topic: Topic name

        Returns:
            Stream key in format "stream:topic:{topic}"
        """
        return f"stream:topic:{topic}"

    def _get_metadata_key(self, topic: str) -> str:
        """Get the Valkey hash key for topic metadata.

        Args:
            topic: Topic name

        Returns:
            Metadata key in format "meta:topic:{topic}"
        """
        return f"meta:topic:{topic}"

    async def save_message(
        self,
        message_id: str,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> None:
        """Save a message to Valkey Stream.

        Args:
            message_id: Unique message identifier
            topic: Topic name
            payload: Message payload
            timestamp: Message timestamp
            metadata: Optional message metadata
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(topic)

        # Prepare stream entry as list of tuples (GLIDE API requirement)
        # Valkey Streams stores fields as key-value pairs
        fields: list[tuple[Union[str, bytes], Union[str, bytes]]] = [
            ("message_id", message_id),
            ("payload", json.dumps(payload)),
            ("timestamp", timestamp.isoformat()),
        ]

        if metadata:
            fields.append(("metadata", json.dumps(metadata)))

        try:
            # Add message to stream with auto-generated ID
            # XADD returns the stream entry ID (e.g., b"1234567890123-0")
            stream_entry_id = await self._client.xadd(stream_key, fields)

            # Trim stream to max length
            # Note: Using exact=True for predictable behavior. Approximate trimming
            # (exact=False) may not trim at all in some cases with Valkey GLIDE.
            await self._client.xtrim(
                stream_key,
                TrimByMaxLen(exact=True, threshold=self.max_messages_per_topic),
            )

            logger.debug(
                f"Saved message {message_id} to topic {topic} with stream ID {stream_entry_id.decode() if stream_entry_id else None}"
            )

        except Exception as e:
            logger.error(f"Failed to save message to Valkey: {e}")
            raise

    async def get_messages(self, topic: str, since: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieve messages from Valkey Stream.

        Args:
            topic: Topic name
            since: Stream ID to start from (exclusive), or None for beginning
            limit: Maximum number of messages to retrieve

        Returns:
            List of message dictionaries
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(topic)

        try:
            # Determine starting bound
            # MinId() for beginning, or ExclusiveIdBound(id) for pagination (skip the provided ID)
            start_bound = ExclusiveIdBound(since) if since else MinId()

            # XRANGE returns messages from start to end
            # MaxId() means to the end of the stream
            stream_entries = await self._client.xrange(stream_key, start=start_bound, end=MaxId(), count=limit)

            messages = []
            if stream_entries:
                # stream_entries is a Mapping[bytes, List[List[bytes]]]
                # Keys are stream IDs (bytes), values are list of [field, value] pairs
                for entry_id_bytes, field_value_list in stream_entries.items():
                    # Convert field-value pairs to dict
                    # Each pair is [field_name_bytes, field_value_bytes]
                    fields = {}
                    for pair in field_value_list:
                        field_name = pair[0].decode('utf-8')
                        field_value = pair[1].decode('utf-8')
                        fields[field_name] = field_value

                    # Parse the fields back into a message dict
                    message = {
                        "message_id": fields.get("message_id", ""),
                        "topic": topic,
                        "payload": json.loads(fields.get("payload", "{}")),
                        "timestamp": fields.get("timestamp", ""),
                        "stream_id": entry_id_bytes.decode('utf-8'),  # Include stream ID for pagination
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

    async def trim_topic(self, topic: str, keep_count: int) -> int:
        """Trim a topic to keep only the most recent messages.

        Args:
            topic: Topic name
            keep_count: Number of messages to keep

        Returns:
            Number of messages removed
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(topic)

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

    async def get_topic_length(self, topic: str) -> int:
        """Get the number of messages in a topic.

        Args:
            topic: Topic name

        Returns:
            Number of messages in the topic
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        stream_key = self._get_stream_key(topic)

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

    async def clear(self) -> None:
        """Clear all messages from all topics (for testing only).

        WARNING: This deletes all stream data.
        """
        if not self._client:
            raise RuntimeError("Not connected to Valkey")

        try:
            await self._client.flushall()

        except Exception as e:
            logger.error(f"Failed to clear Valkey data: {e}")
            raise
