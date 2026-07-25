"""In-memory storage backend for testing and hot-tier caching."""

import asyncio
import time
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
        self._messages: dict[str, deque] = defaultdict(deque)
        self._lock: Optional[asyncio.Lock] = None if version_info < (3, 10) else asyncio.Lock()
        self._max_messages = max_messages_per_topic
        self._consumer_groups: dict[str, str] = {}
        self._group_cursors: dict[tuple[str, str], Optional[str]] = {}
        self._pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._ordering_locks: dict[tuple[str, str, str], str] = {}
        self._dedupe_tombstones: dict[tuple[str, str, str], float] = {}

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
            # Grouped streams cannot discard entries which have not yet been
            # assigned. Pending entries are retained independently as well.
            if key not in self._consumer_groups:
                while len(self._messages[key]) > self._max_messages:
                    self._messages[key].popleft()

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

    async def get_consumer_group(self, owner_id: str, topic: str) -> Optional[str]:
        async with self._get_lock():
            return self._consumer_groups.get(self._key(owner_id, topic))

    def _expire_tombstones(self, now: float) -> None:
        for key, expires_at in list(self._dedupe_tombstones.items()):
            if expires_at <= now:
                del self._dedupe_tombstones[key]

    async def poll_group(
        self,
        owner_id: str,
        topics: list[str],
        group: str,
        consumer: str,
        limit: int,
        visibility_timeout: int,
    ) -> list[dict[str, Any]]:
        from pulsar_relay.storage.base import ConsumerGroupConflictError

        now = time.monotonic()
        claimed: list[dict[str, Any]] = []
        async with self._get_lock():
            self._expire_tombstones(now)
            for topic in topics:
                key = self._key(owner_id, topic)
                existing_group = self._consumer_groups.get(key)
                if existing_group is None:
                    self._consumer_groups[key] = group
                    existing_messages = self._messages.get(key, ())
                    self._group_cursors[(key, group)] = (
                        existing_messages[-1]["message_id"] if existing_messages else None
                    )
                elif existing_group != group:
                    raise ConsumerGroupConflictError(
                        f"Topic {topic!r} is already assigned to consumer group {existing_group!r}"
                    )

            # Reclaim expired work first. The ordering lock identifies the one
            # entry for a job that is currently allowed to execute.
            for pending_key, pending in list(self._pending.items()):
                if len(claimed) >= limit:
                    break
                key, pending_group, message_id = pending_key
                if pending_group != group or key not in {self._key(owner_id, topic) for topic in topics}:
                    continue
                metadata = pending["message"].get("metadata") or {}
                dedupe_key = metadata.get("deduplication_key")
                if dedupe_key and (key, group, dedupe_key) in self._dedupe_tombstones:
                    del self._pending[pending_key]
                    continue
                ordering_key = pending.get("ordering_key")
                lock_key = (key, group, ordering_key) if ordering_key else None
                lock_owner = self._ordering_locks.get(lock_key) if lock_key else None
                expired = now - pending["delivered_at"] >= visibility_timeout
                blocked = lock_key is not None and lock_owner != message_id
                if not expired and not blocked:
                    continue
                if blocked and lock_owner is not None:
                    continue
                pending["consumer"] = consumer
                pending["delivered_at"] = now
                pending["delivery_count"] += 1
                if lock_key:
                    self._ordering_locks[lock_key] = message_id
                claimed.append(dict(pending["message"]))

            for topic in topics:
                if len(claimed) >= limit:
                    break
                key = self._key(owner_id, topic)
                messages = list(self._messages.get(key, ()))
                cursor_key = (key, group)
                cursor = self._group_cursors.get(cursor_key)
                start = 0
                if cursor:
                    for index, message in enumerate(messages):
                        if message["message_id"] == cursor:
                            start = index + 1
                            break
                for message in messages[start:]:
                    self._group_cursors[cursor_key] = message["message_id"]
                    metadata = message.get("metadata") or {}
                    dedupe_key = metadata.get("deduplication_key")
                    if dedupe_key and (key, group, dedupe_key) in self._dedupe_tombstones:
                        continue
                    message_id = message["message_id"]
                    ordering_key = metadata.get("ordering_key")
                    lock_key = (key, group, ordering_key) if ordering_key else None
                    pending = {
                        "consumer": consumer,
                        "delivered_at": now,
                        "delivery_count": 1,
                        "ordering_key": ordering_key,
                        "message": dict(message),
                    }
                    self._pending[(key, group, message_id)] = pending
                    if lock_key and lock_key in self._ordering_locks:
                        continue
                    if lock_key:
                        self._ordering_locks[lock_key] = message_id
                    claimed.append(dict(message))
                    if len(claimed) >= limit:
                        break

        return claimed

    async def update_group_deliveries(
        self,
        owner_id: str,
        group: str,
        consumer: str,
        deliveries: list[dict[str, str]],
        action: str,
        visibility_timeout: int,
    ) -> int:
        updated = 0
        now = time.monotonic()
        async with self._get_lock():
            for delivery in deliveries:
                key = self._key(owner_id, delivery["topic"])
                pending_key = (key, group, delivery["message_id"])
                pending = self._pending.get(pending_key)
                if pending is None or pending["consumer"] != consumer:
                    continue
                ordering_key = pending.get("ordering_key")
                lock_key = (key, group, ordering_key) if ordering_key else None
                if lock_key and self._ordering_locks.get(lock_key) != delivery["message_id"]:
                    continue
                if action == "touch":
                    pending["delivered_at"] = now
                else:
                    metadata = pending["message"].get("metadata") or {}
                    dedupe_key = metadata.get("deduplication_key")
                    if dedupe_key:
                        self._dedupe_tombstones[(key, group, dedupe_key)] = now + 7 * 24 * 60 * 60
                    del self._pending[pending_key]
                    if lock_key:
                        self._ordering_locks.pop(lock_key, None)
                updated += 1
        return updated

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
            self._consumer_groups.clear()
            self._group_cursors.clear()
            self._pending.clear()
            self._ordering_locks.clear()
            self._dedupe_tombstones.clear()
