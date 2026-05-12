"""In-memory storage backend for testing and hot-tier caching."""

import asyncio
import uuid
from collections import defaultdict, deque
from datetime import datetime
from sys import version_info
from typing import Any, Optional

from pulsar_relay.storage.base import StorageBackend


class MemoryStorage(StorageBackend):
    """In-memory storage using deque for message buffering."""

    def __init__(self, max_messages_per_topic: int = 10000):
        """Initialize memory storage.

        Args:
            max_messages_per_topic: Maximum messages to store per topic
        """
        self._messages: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_messages_per_topic))
        self._lock: Optional[asyncio.Lock] = None if version_info < (3, 10) else asyncio.Lock()
        self._max_messages = max_messages_per_topic

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the asyncio lock.

        This is lazily initialized to avoid issues with event loop
        not being available during __init__ in Python 3.9.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _key(owner_id: str, topic: str) -> str:
        """Compose the per-owner namespaced storage key."""
        return f"{owner_id}/{topic}"

    async def save_message(
        self,
        owner_id: str,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Save a message to in-memory storage.

        Returns:
            Generated message ID (UUID-based)
        """
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        key = self._key(owner_id, topic)

        async with self._get_lock():
            message = {
                "message_id": message_id,
                "topic": topic,
                "payload": payload,
                "timestamp": timestamp.isoformat(),
                "metadata": metadata or {},
            }
            self._messages[key].append(message)

        return message_id

    async def get_messages(
        self,
        owner_id: str,
        topic: str,
        since: Optional[str] = None,
        limit: int = 10,
        reverse: bool = False,
    ) -> list[dict[str, Any]]:
        """Get messages from ``owner_id``'s topic."""
        key = self._key(owner_id, topic)
        async with self._get_lock():
            if key not in self._messages:
                return []

            messages = list(self._messages[key])

            if reverse:
                messages.reverse()

            if since:
                try:
                    since_idx = next(i for i, msg in enumerate(messages) if msg["message_id"] == since)
                    messages = messages[since_idx + 1 :]
                except StopIteration:
                    pass

            return messages[:limit]

    async def trim_topic(self, owner_id: str, topic: str, max_messages: int) -> int:
        """Trim old messages from a topic."""
        key = self._key(owner_id, topic)
        async with self._get_lock():
            if key not in self._messages:
                return 0

            messages = self._messages[key]
            current_length = len(messages)
            if current_length <= max_messages:
                return 0

            messages_to_remove = current_length - max_messages
            for _ in range(messages_to_remove):
                messages.popleft()
            return messages_to_remove

    async def get_topic_length(self, owner_id: str, topic: str) -> int:
        """Get the number of messages in a topic."""
        key = self._key(owner_id, topic)
        async with self._get_lock():
            return len(self._messages.get(key, []))

    async def health_check(self) -> dict:
        """Check if storage is healthy."""
        return {"status": "healthy"}

    async def close(self) -> None:
        """Close storage (no-op for memory storage)."""
        pass

    async def clear(self) -> None:
        """Clear all messages (for testing)."""
        async with self._get_lock():
            self._messages.clear()
