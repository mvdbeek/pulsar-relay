"""Long polling manager for handling HTTP long polling clients."""

import asyncio
import datetime
import logging
from collections import defaultdict
from sys import version_info
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class PollWaiterLimitExceededError(Exception):
    """Raised when a user attempts to create more concurrent poll
    waiters than :attr:`PollManager._max_waiters_per_user`."""


# Bound the per-waiter queue. ``broadcast_to_topic`` calls ``put_message``
# without backpressure, so an abandoned waiter with hundreds of topic
# subscriptions would otherwise accumulate state without bound. 1024
# pending messages per waiter is enough for any normal client; beyond
# that we drop the message and log — clients that miss a beat can
# resync via the ``since=`` cursor on the next poll.
_DEFAULT_WAITER_QUEUE_MAXSIZE = 1024


class PollWaiter:
    """Represents a client waiting for messages via long polling."""

    def __init__(
        self,
        client_id: str,
        topics: list[str],
        user_id: Optional[str] = None,
        queue_maxsize: int = _DEFAULT_WAITER_QUEUE_MAXSIZE,
    ):
        """Initialize a poll waiter.

        Args:
            client_id: Unique identifier for the polling client
            topics: List of topics the client is subscribed to
            user_id: Authenticated user the waiter belongs to (used for
                per-user concurrent-waiter caps).
            queue_maxsize: Cap on pending messages buffered for this
                waiter before further messages are dropped.
        """
        self.client_id = client_id
        self.user_id = user_id
        self.topics = set(topics)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        # use timezone.utc to be explicit and mypy-friendly
        self.created_at = datetime.datetime.now(datetime.timezone.utc)

    async def put_message(self, message: dict[str, Any]) -> bool:
        """Add a message to the waiter's queue.

        Args:
            message: Message to add to queue

        Returns:
            True if the message was queued; False if the queue is full
            (the waiter is abandoned or slow). The caller can use this
            signal to decide whether to evict the waiter.
        """
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "PollWaiter %s queue full (maxsize=%d); dropping message",
                self.client_id,
                self.queue.maxsize,
            )
            return False

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

    def __init__(self, max_waiters_per_user: int = 50):
        """Initialize the poll manager.

        Args:
            max_waiters_per_user: Cap on concurrent waiters for a single
                authenticated user. Defends against a single caller
                exhausting the waiter pool (API H#8).
        """
        self._waiters: dict[str, PollWaiter] = {}
        self._topic_subscribers: dict[str, set[str]] = defaultdict(set)
        self._per_user_count: dict[str, int] = defaultdict(int)
        self._max_waiters_per_user = max_waiters_per_user
        self._lock: Optional[asyncio.Lock] = None if version_info < (3, 10) else asyncio.Lock()

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the asyncio lock.

        This is lazily initialized to avoid issues with event loop
        not being available during __init__ in Python 3.9.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def create_waiter(self, topics: list[str], user_id: Optional[str] = None) -> PollWaiter:
        """Create a new poll waiter for the given topics.

        Args:
            topics: List of topics to subscribe to
            user_id: Authenticated user the waiter belongs to. When
                provided, the per-user concurrent-waiter cap is
                enforced.

        Returns:
            PollWaiter instance

        Raises:
            PollWaiterLimitExceededError: If ``user_id`` already has
                ``max_waiters_per_user`` waiters in flight.
        """
        client_id = str(uuid4())
        waiter = PollWaiter(client_id, topics, user_id=user_id)

        async with self._get_lock():
            if user_id is not None and self._per_user_count[user_id] >= self._max_waiters_per_user:
                raise PollWaiterLimitExceededError(
                    f"user {user_id!r} already has {self._per_user_count[user_id]} concurrent poll waiters; "
                    f"limit is {self._max_waiters_per_user}"
                )
            self._waiters[client_id] = waiter
            if user_id is not None:
                self._per_user_count[user_id] += 1
            for topic in topics:
                self._topic_subscribers[topic].add(client_id)

        logger.info(f"Created poll waiter {client_id} for topics: {topics}")

        return waiter

    async def remove_waiter(self, client_id: str) -> None:
        """Remove a poll waiter.

        Args:
            client_id: ID of the waiter to remove
        """
        async with self._get_lock():
            waiter = self._waiters.pop(client_id, None)
            if waiter:
                # Remove from topic subscribers
                for topic in waiter.topics:
                    self._topic_subscribers[topic].discard(client_id)
                    # Clean up empty topic sets
                    if not self._topic_subscribers[topic]:
                        del self._topic_subscribers[topic]

                # Decrement the per-user counter.
                if waiter.user_id is not None:
                    self._per_user_count[waiter.user_id] -= 1
                    if self._per_user_count[waiter.user_id] <= 0:
                        del self._per_user_count[waiter.user_id]

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
        async with self._get_lock():
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

        async with self._get_lock():
            for client_id, waiter in self._waiters.items():
                age = (now - waiter.created_at).total_seconds()
                if age > max_age_seconds:
                    stale_ids.append(client_id)

        for client_id in stale_ids:
            await self.remove_waiter(client_id)

        if stale_ids:
            logger.info(f"Cleaned up {len(stale_ids)} stale poll waiters")

        return len(stale_ids)

    async def cleanup_loop(self, interval_seconds: int = 60, max_age_seconds: int = 300) -> None:
        """Run :meth:`cleanup_stale_waiters` on a fixed interval.

        Intended to be scheduled from the FastAPI ``lifespan`` startup
        as ``asyncio.create_task(poll_manager.cleanup_loop())``. The
        task exits cleanly on cancellation; otherwise it runs forever.
        Closes API H#8 (``cleanup_stale_waiters`` was defined but
        never invoked).
        """
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self.cleanup_stale_waiters(max_age_seconds=max_age_seconds)
                except Exception:
                    # Don't let a single sweep failure kill the loop.
                    logger.exception("PollManager cleanup sweep failed; continuing")
        except asyncio.CancelledError:
            logger.info("PollManager cleanup loop cancelled — shutting down")
            raise
