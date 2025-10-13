"""Base storage interface."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    async def save_message(
        self,
        message_id: str,
        topic: str,
        payload: dict[str, Any],
        timestamp: datetime,
        metadata: Optional[dict[str, str]] = None,
    ) -> None:
        """Save a message to storage."""
        pass

    @abstractmethod
    async def get_messages(self, topic: str, since: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        """Get messages from a topic."""
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
