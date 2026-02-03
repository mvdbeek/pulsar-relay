"""Base storage interface."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    async def save_message(
        self,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Save a message to storage.

        Returns:
            The message ID assigned by the storage backend.
            For Valkey, this is the stream ID.
            For in-memory storage, this is a generated UUID.
        """
        pass

    @abstractmethod
    async def get_messages(
        self, topic: str, since: Optional[str] = None, limit: int = 10, reverse: bool = False
    ) -> list[dict[str, Any]]:
        """Get messages from a topic.

        Args:
            topic: Topic name
            since: Message ID to start from (exclusive)
            limit: Maximum number of messages to return
            reverse: If True, return messages in reverse chronological order (newest first)

        Returns:
            List of message dictionaries
        """
        pass

    @abstractmethod
    async def trim_topic(self, topic: str, max_messages: int) -> int:
        """Trim old messages from a topic. Returns number of messages removed."""
        pass

    @abstractmethod
    async def get_topic_length(self, topic: str) -> int:
        """Get the number of messages in a topic."""
        pass

    @abstractmethod
    async def health_check(self) -> dict:
        """Check if storage backend is healthy."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close storage connections."""
        pass
