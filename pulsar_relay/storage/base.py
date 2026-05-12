"""Base storage interface."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    async def save_message(
        self,
        owner_id: str,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Save a message to ``owner_id``'s topic.

        Two different users with the same ``topic`` name have entirely
        separate streams — the storage key is composed from
        ``(owner_id, topic)``.

        Returns:
            The message ID assigned by the storage backend.
            For Valkey, this is the stream ID.
            For in-memory storage, this is a generated UUID.
        """
        pass

    @abstractmethod
    async def get_messages(
        self,
        owner_id: str,
        topic: str,
        since: Optional[str] = None,
        limit: int = 10,
        reverse: bool = False,
    ) -> list[dict[str, Any]]:
        """Get messages from ``owner_id``'s topic."""
        pass

    @abstractmethod
    async def trim_topic(self, owner_id: str, topic: str, max_messages: int) -> int:
        """Trim old messages from a topic. Returns number of messages removed."""
        pass

    @abstractmethod
    async def get_topic_length(self, owner_id: str, topic: str) -> int:
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
