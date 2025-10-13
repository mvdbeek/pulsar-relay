"""Long polling manager for handling HTTP long polling clients."""

import asyncio
import datetime
import logging
from collections import defaultdict
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class PollWaiter:
    """Represents a client waiting for messages via long polling."""

    def __init__(self, client_id: str, topics: list[str]):
        """Initialize a poll waiter.

        Args:
            client_id: Unique identifier for the polling client
            topics: List of topics the client is subscribed to
        """
        self.client_id = client_id
        self.topics = set(topics)
        self.queue: asyncio.Queue = asyncio.Queue()
        # use timezone.utc to be explicit and mypy-friendly
        self.created_at = datetime.datetime.now(datetime.timezone.utc)

    async def put_message(self, message: dict[str, Any]) -> None:
        """Add a message to the waiter's queue.

        Args:
            message: Message to add to queue
        """
        await self.queue.put(message)

    async def wait_for_messages(self, timeout: float) -> list[dict[str, Any]]:
        """Wait for messages with timeout.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            List of messages received
        """
        messages = []
        try:
            # Wait for first message with timeout
            first_message = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            messages.append(first_message)

            # Collect any additional messages that are immediately available
            while not self.queue.empty():
                try:
                    message = self.queue.get_nowait()
                    messages.append(message)
                except asyncio.QueueEmpty:
                    break

        except asyncio.TimeoutError:
            # No messages received within timeout, return empty list
            pass

        return messages


class PollManager:
    """Manages long polling clients and message distribution."""

    def __init__(self):
        """Initialize the poll manager."""
        self._waiters: dict[str, PollWaiter] = {}
        self._topic_subscribers: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def create_waiter(self, topics: list[str]) -> PollWaiter:
        """Create a new poll waiter for the given topics.

        Args:
            topics: List of topics to subscribe to

        Returns:
            PollWaiter instance
        """
        client_id = str(uuid4())
        waiter = PollWaiter(client_id, topics)

        async with self._lock:
            self._waiters[client_id] = waiter
            for topic in topics:
                self._topic_subscribers[topic].add(client_id)

        logger.info(f"Created poll waiter {client_id} for topics: {topics}")

        return waiter

    async def remove_waiter(self, client_id: str) -> None:
        """Remove a poll waiter.

        Args:
            client_id: ID of the waiter to remove
        """
        async with self._lock:
            waiter = self._waiters.pop(client_id, None)
            if waiter:
                # Remove from topic subscribers
                for topic in waiter.topics:
                    self._topic_subscribers[topic].discard(client_id)
                    # Clean up empty topic sets
                    if not self._topic_subscribers[topic]:
                        del self._topic_subscribers[topic]

                logger.info(f"Removed poll waiter {client_id}")

    async def broadcast_to_topic(self, topic: str, message: dict[str, Any]) -> int:
        """Broadcast a message to all waiters subscribed to a topic.

        Args:
            topic: Topic to broadcast to
            message: Message to broadcast

        Returns:
            Number of waiters that received the message
        """
        count = 0
        async with self._lock:
            client_ids = self._topic_subscribers.get(topic, set()).copy()

        for client_id in client_ids:
            waiter = self._waiters.get(client_id)
            if waiter:
                await waiter.put_message(message)
                count += 1

        if count > 0:
            logger.debug(f"Broadcasted message to {count} poll waiters on topic {topic}")

        return count

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about active poll waiters.

        Returns:
            Dictionary with statistics
        """
        return {
            "active_waiters": len(self._waiters),
            "subscribed_topics": len(self._topic_subscribers),
            "topic_subscriber_counts": {
                topic: len(subscribers) for topic, subscribers in self._topic_subscribers.items()
            },
        }

    async def cleanup_stale_waiters(self, max_age_seconds: int = 300) -> int:
        """Remove waiters that have been waiting too long.

        Args:
            max_age_seconds: Maximum age in seconds before considering stale

        Returns:
            Number of waiters cleaned up
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_ids = []

        async with self._lock:
            for client_id, waiter in self._waiters.items():
                age = (now - waiter.created_at).total_seconds()
                if age > max_age_seconds:
                    stale_ids.append(client_id)

        for client_id in stale_ids:
            await self.remove_waiter(client_id)

        if stale_ids:
            logger.info(f"Cleaned up {len(stale_ids)} stale poll waiters")

        return len(stale_ids)
