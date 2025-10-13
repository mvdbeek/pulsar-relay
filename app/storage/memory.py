"""In-memory storage backend for testing and hot-tier caching."""

import asyncio
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Optional

from app.storage.base import StorageBackend


class MemoryStorage(StorageBackend):
    """In-memory storage using deque for message buffering."""

    def __init__(self, max_messages_per_topic: int = 10000):
        """Initialize memory storage.

        Args:
            max_messages_per_topic: Maximum messages to store per topic
        """
        self._messages: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_messages_per_topic))
        self._lock = asyncio.Lock()
        self._max_messages = max_messages_per_topic

    async def save_message(
        self,
        message_id: str,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> None:
        """Save a message to in-memory storage."""
        async with self._lock:
            message = {
                "message_id": message_id,
                "topic": topic,
                "payload": payload,
                "timestamp": timestamp.isoformat(),
                "metadata": metadata or {},
            }
            self._messages[topic].append(message)

    async def get_messages(self, topic: str, since: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        """Get messages from a topic.

        Args:
            topic: Topic name
            since: Message ID to start from (exclusive)
            limit: Maximum number of messages to return

        Returns:
            List of messages
        """
        async with self._lock:
            if topic not in self._messages:
                return []

            messages = list(self._messages[topic])

            # Filter by since if provided
            if since:
                try:
                    # Find the index of the since message
                    since_idx = next(i for i, msg in enumerate(messages) if msg["message_id"] == since)
                    # Return messages after the since message
                    messages = messages[since_idx + 1 :]
                except StopIteration:
                    # If since message not found, return all messages
                    pass

            # Limit the number of messages
            return messages[:limit]

    async def trim_topic(self, topic: str, max_messages: int) -> int:
        """Trim old messages from a topic."""
        async with self._lock:
            if topic not in self._messages:
                return 0

            messages = self._messages[topic]
            current_length = len(messages)

            if current_length <= max_messages:
                return 0

            # Remove oldest messages
            messages_to_remove = current_length - max_messages
            for _ in range(messages_to_remove):
                messages.popleft()

            return messages_to_remove

    async def get_topic_length(self, topic: str) -> int:
        """Get the number of messages in a topic."""
        async with self._lock:
            return len(self._messages.get(topic, []))

    async def health_check(self) -> dict:
        """Check if storage is healthy."""
        return {"status": "healthy"}

    async def close(self) -> None:
        """Close storage (no-op for memory storage)."""
        pass

    async def clear(self) -> None:
        """Clear all messages (for testing)."""
        async with self._lock:
            self._messages.clear()
