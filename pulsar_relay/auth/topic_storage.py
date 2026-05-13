"""Topic storage backends for managing topic ownership.

Topics are namespaced by ``(owner_id, topic_name)`` so two different
users can both have a topic called ``"jobs"`` without colliding
(API H#5 — Phase 3c).

Cross-user access (``is_public`` flag, ``allowed_user_ids`` set,
``grant_access`` / ``revoke_access`` calls) was removed in Phase 4
(security review follow-up): with per-user namespacing the wire
contract no longer addresses any topic outside the bearer's
namespace, so those code paths were unreachable. A future
reintroduction would need a wire mechanism to specify an explicit
owner (e.g. ``?owner=alice`` on the topic-detail GET).
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pulsar_relay.auth.models import Topic, TopicCreate

logger = logging.getLogger(__name__)


class TopicStorage(ABC):
    """Abstract base class for topic storage."""

    @abstractmethod
    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        """Create a new topic owned by ``owner_id``.

        Raises ``ValueError`` if this user already owns a topic by
        this name.
        """
        pass

    @abstractmethod
    async def get_topic(self, owner_id: str, topic_name: str) -> Optional[Topic]:
        """Get one user's topic by name."""
        pass

    @abstractmethod
    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        """List topics owned by a user."""
        pass

    @abstractmethod
    async def update_topic(
        self,
        owner_id: str,
        topic_name: str,
        description: Optional[str] = None,
    ) -> Optional[Topic]:
        """Update topic metadata."""
        pass

    @abstractmethod
    async def delete_topic(self, owner_id: str, topic_name: str) -> bool:
        """Delete a topic owned by ``owner_id``."""
        pass

    @abstractmethod
    async def user_can_access(
        self,
        owner_id: str,
        topic_name: str,
        user_id: str,
        permission_type: Literal["read", "write"],
        user_permissions: Sequence[str],
    ) -> bool:
        """Check whether ``user_id`` can ``permission_type`` ``owner_id``'s
        ``topic_name``.

        Resolution order:
        1. ``"admin"`` in ``user_permissions`` → True.
        2. Topic doesn't exist → True (auto-create semantics).
        3. ``user_id == owner_id`` → True.
        4. Otherwise → False.

        The previous shared-access semantics (``allowed_user_ids``,
        ``is_public``) are gone. With per-user namespacing the wire
        contract resolves every topic operation to ``(bearer.sub,
        name)``, so cross-user access has no reachable code path.
        """
        pass

    # Compatibility shim: list of topics accessible to the user.
    # Without cross-user access this is exactly the owned topics.
    async def list_user_topics(self, user_id: str) -> list[Topic]:
        return await self.list_owned_topics(user_id)

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        return {}


# Type alias for the composite key used by in-memory storage.
_TopicKey = tuple[str, str]  # (owner_id, topic_name)


class InMemoryTopicStorage(TopicStorage):
    """In-memory topic storage for development/testing."""

    def __init__(self) -> None:
        # Composite key: (owner_id, topic_name) -> Topic.
        self._topics: dict[_TopicKey, Topic] = {}
        # user_id -> set of topic_names this user owns.
        self._owner_index: dict[str, set[str]] = {}
        logger.info("Initialized InMemoryTopicStorage")

    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        key = (owner_id, topic_data.topic_name)
        if key in self._topics:
            raise ValueError(f"Topic {topic_data.topic_name!r} already exists for owner {owner_id}")

        topic = Topic(
            topic_id=str(uuid4()),
            topic_name=topic_data.topic_name,
            owner_id=owner_id,
            description=topic_data.description,
            created_at=datetime.now(timezone.utc),
        )

        self._topics[key] = topic
        self._owner_index.setdefault(owner_id, set()).add(topic_data.topic_name)
        logger.info("Created topic: %s (owner: %s)", topic_data.topic_name, owner_id)
        return topic

    async def get_topic(self, owner_id: str, topic_name: str) -> Optional[Topic]:
        return self._topics.get((owner_id, topic_name))

    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        topic_names = self._owner_index.get(user_id, set())
        return [self._topics[(user_id, n)] for n in topic_names if (user_id, n) in self._topics]

    async def update_topic(
        self,
        owner_id: str,
        topic_name: str,
        description: Optional[str] = None,
    ) -> Optional[Topic]:
        topic = self._topics.get((owner_id, topic_name))
        if not topic:
            return None
        if description is not None:
            topic.description = description
        logger.info("Updated topic: %s (owner %s)", topic_name, owner_id)
        return topic

    async def delete_topic(self, owner_id: str, topic_name: str) -> bool:
        topic = self._topics.pop((owner_id, topic_name), None)
        if not topic:
            return False
        self._owner_index.get(owner_id, set()).discard(topic_name)
        logger.info("Deleted topic: %s (owner %s)", topic_name, owner_id)
        return True

    async def user_can_access(
        self,
        owner_id: str,
        topic_name: str,
        user_id: str,
        permission_type: Literal["read", "write"],
        user_permissions: Sequence[str],
    ) -> bool:
        if "admin" in user_permissions:
            return True
        topic = self._topics.get((owner_id, topic_name))
        if not topic:
            # Auto-create semantics: caller will create the topic
            # under owner_id. Closed at the (owner_id, name) layer.
            return True
        return topic.owner_id == user_id

    async def get_stats(self) -> dict[str, Any]:
        return {"total_topics": len(self._topics)}


# ---------- Valkey ----------


# Key layout (all namespaced by owner_id):
#   topic:{owner_id}/{name}     — hash (Topic record)
#   user:{user_id}:owned_topics — set of names this user owns
_OWNED_PREFIX = "topic:"
# Suffix retained as a constant only so the legacy-key migration check
# (``scan_for_legacy_keys`` below) can ignore the corresponding old
# keys cleanly. The relay itself no longer writes them.
_ALLOWED_USERS_SUFFIX = ":allowed_users"


def _owner_topic_key(owner_id: str, topic_name: str) -> str:
    return f"{_OWNED_PREFIX}{owner_id}/{topic_name}"


def _user_owned_key(user_id: str) -> str:
    return f"user:{user_id}:owned_topics"


class ValkeyTopicStorage(TopicStorage):
    """Valkey-based topic storage for production use."""

    def __init__(self, client) -> None:
        self._client = client
        logger.info("Initialized ValkeyTopicStorage")

    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        topic_key = _owner_topic_key(owner_id, topic_data.topic_name)

        topic = Topic(
            topic_id=str(uuid4()),
            topic_name=topic_data.topic_name,
            owner_id=owner_id,
            description=topic_data.description,
            created_at=datetime.now(timezone.utc),
        )

        # Atomic create via HSETNX on the sentinel topic_id field.
        created = await self._client.hsetnx(topic_key, "topic_id", topic.topic_id)
        if not created:
            raise ValueError(f"Topic {topic_data.topic_name!r} already exists for owner {owner_id}")

        await self._client.hset(
            topic_key,
            {
                "topic_name": topic.topic_name,
                "owner_id": topic.owner_id,
                "description": topic.description or "",
                "created_at": topic.created_at.isoformat(),
            },
        )
        await self._client.sadd(_user_owned_key(owner_id), [topic.topic_name])

        logger.info("Created topic in Valkey: %s (owner: %s)", topic.topic_name, owner_id)
        return topic

    async def get_topic(self, owner_id: str, topic_name: str) -> Optional[Topic]:
        topic_key = _owner_topic_key(owner_id, topic_name)
        topic_hash = await self._client.hgetall(topic_key)
        if not topic_hash:
            return None

        data = {k.decode("utf-8"): v.decode("utf-8") for k, v in topic_hash.items()}
        return Topic(
            topic_id=data["topic_id"],
            topic_name=data["topic_name"],
            owner_id=data["owner_id"],
            description=data.get("description") or None,
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        names_bytes = await self._client.smembers(_user_owned_key(user_id))
        if not names_bytes:
            return []
        topics: list[Topic] = []
        for raw in names_bytes:
            name = raw.decode("utf-8")
            topic = await self.get_topic(user_id, name)
            if topic is not None:
                topics.append(topic)
        return topics

    async def update_topic(
        self,
        owner_id: str,
        topic_name: str,
        description: Optional[str] = None,
    ) -> Optional[Topic]:
        topic_key = _owner_topic_key(owner_id, topic_name)
        if (await self._client.exists([topic_key])) == 0:
            return None

        if description is not None:
            await self._client.hset(topic_key, [("description", description)])
        logger.info("Updated topic in Valkey: %s (owner %s)", topic_name, owner_id)
        return await self.get_topic(owner_id, topic_name)

    async def delete_topic(self, owner_id: str, topic_name: str) -> bool:
        topic = await self.get_topic(owner_id, topic_name)
        if not topic:
            return False

        await self._client.delete([_owner_topic_key(owner_id, topic_name)])
        await self._client.srem(_user_owned_key(owner_id), [topic_name])
        logger.info("Deleted topic from Valkey: %s (owner %s)", topic_name, owner_id)
        return True

    async def user_can_access(
        self,
        owner_id: str,
        topic_name: str,
        user_id: str,
        permission_type: Literal["read", "write"],
        user_permissions: Sequence[str],
    ) -> bool:
        if "admin" in user_permissions:
            return True
        topic = await self.get_topic(owner_id, topic_name)
        if not topic:
            return True
        return topic.owner_id == user_id

    async def get_stats(self) -> dict[str, Any]:
        return {
            "storage_type": "valkey",
            "message": "Stats require scanning all keys (expensive operation)",
        }


async def scan_for_legacy_keys(client, *, limit: int = 100) -> list[str]:
    """Find pre-Phase-3c topic keys still present in Valkey.

    Returns up to ``limit`` example keys whose name portion lacks a
    ``/`` (and is therefore from the pre-namespacing flat layout).
    The startup migration check in ``main.py`` calls this and refuses
    to boot when any are present (unless ``allow_insecure_defaults``).

    Keys with the post-Phase-4 ``:allowed_users`` suffix are also
    flagged here as legacy — Phase 4 dropped the shared-access
    feature, so any such keys lingering in the store are also stale.
    """
    prefixes = ["topic:", "stream:topic:", "meta:topic:"]
    examples: list[str] = []
    for prefix in prefixes:
        cursor: Any = b"0"
        while True:
            result = await client.scan(cursor, match=f"{prefix}*", count=200)
            cursor, raw_keys = result[0], result[1]
            for raw in raw_keys:
                key = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
                payload = key[len(prefix) :]
                # The legacy ``:allowed_users`` suffix marks a key from
                # the dropped shared-access feature; it is also legacy
                # whether or not the owner segment is present.
                if payload.endswith(_ALLOWED_USERS_SUFFIX):
                    examples.append(key)
                    if len(examples) >= limit:
                        return examples
                    continue
                if "/" not in payload:
                    examples.append(key)
                    if len(examples) >= limit:
                        return examples
            if cursor in (b"0", "0", 0):
                break
    return examples
