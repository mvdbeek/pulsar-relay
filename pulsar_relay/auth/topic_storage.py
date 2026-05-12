"""Topic storage backends for managing topic ownership and permissions.

Topics are namespaced by ``(owner_id, topic_name)`` so two different
users can both have a topic called ``"jobs"`` without colliding. This
closes the squatting risk described in security review API H#5: under
the previous flat namespace, the first user to publish to ``"jobs"``
became its owner and locked everyone else out.

Every storage operation that previously took just ``topic_name`` now
takes ``owner_id`` alongside it. API call sites pass the bearer's
``current_user.user_id`` as the owner, which means the relay's wire
contract continues to refer to bare topic names — the namespacing is
internal.

Cross-user access (shared topics via ``allowed_user_ids``) is preserved
in the storage layer (the access check still consults the allow-list)
but cannot currently be triggered by the wire contract because no API
path lets a caller specify a topic belonging to a different owner.
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
        """Create a new topic.

        Args:
            owner_id: User ID of the topic owner. The composite key
                ``(owner_id, topic_name)`` makes the topic distinct
                from other users' topics with the same name.
            topic_data: Topic creation data

        Returns:
            Created topic

        Raises:
            ValueError: If this user already owns a topic by this name.
        """
        pass

    @abstractmethod
    async def get_topic(self, owner_id: str, topic_name: str) -> Optional[Topic]:
        """Get one user's topic by name.

        Args:
            owner_id: User ID of the topic owner
            topic_name: Topic name (bare; no owner prefix)

        Returns:
            Topic if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_user_topics(self, user_id: str) -> list[Topic]:
        """List all topics accessible to a user (owned + granted access)."""
        pass

    @abstractmethod
    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        """List topics owned by a user."""
        pass

    @abstractmethod
    async def grant_access(self, owner_id: str, topic_name: str, user_id: str) -> bool:
        """Grant ``user_id`` access to a topic owned by ``owner_id``.

        Returns True if access granted, False if the topic does not
        exist. Raises ``ValueError`` if the user already has access.
        """
        pass

    @abstractmethod
    async def revoke_access(self, owner_id: str, topic_name: str, user_id: str) -> bool:
        """Revoke ``user_id``'s access to a topic owned by ``owner_id``."""
        pass

    @abstractmethod
    async def update_topic(
        self,
        owner_id: str,
        topic_name: str,
        is_public: Optional[bool] = None,
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
        1. ``"admin"`` in user_permissions → True (admin override).
        2. Topic doesn't exist → True (will be auto-created by caller).
        3. ``user_id == owner_id`` → True (owner has full access).
        4. Non-read permission requested by non-owner → False.
        5. ``user_id`` in topic.allowed_user_ids → True (granted read).
        6. ``topic.is_public`` → True.
        7. False.
        """
        pass

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        return {}


# Type alias for the composite key used by in-memory storage. Spelt out
# rather than inlined to keep call sites readable.
_TopicKey = tuple[str, str]  # (owner_id, topic_name)


class InMemoryTopicStorage(TopicStorage):
    """In-memory topic storage for development/testing."""

    def __init__(self) -> None:
        # Composite key: (owner_id, topic_name) -> Topic. The owner
        # prefix means two users can both have "jobs" without
        # colliding, closing the squat risk.
        self._topics: dict[_TopicKey, Topic] = {}
        # user_id -> set of topic_names this user OWNS. Disjoint from
        # ``_allowed_user_topics`` below: a user listed there is a
        # GRANTED reader, not the owner.
        self._owner_index: dict[str, set[str]] = {}
        # user_id -> set of (owner_id, topic_name) the user has been
        # granted access to. Tracked independently so revoking the
        # grant is O(1) lookup.
        self._allowed_user_topics: dict[str, set[_TopicKey]] = {}
        logger.info("Initialized InMemoryTopicStorage")

    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        key = (owner_id, topic_data.topic_name)
        if key in self._topics:
            raise ValueError(f"Topic {topic_data.topic_name!r} already exists for owner {owner_id}")

        topic = Topic(
            topic_id=str(uuid4()),
            topic_name=topic_data.topic_name,
            owner_id=owner_id,
            is_public=topic_data.is_public,
            description=topic_data.description,
            created_at=datetime.now(timezone.utc),
            allowed_user_ids=[],
        )

        self._topics[key] = topic
        self._owner_index.setdefault(owner_id, set()).add(topic_data.topic_name)
        logger.info("Created topic: %s (owner: %s)", topic_data.topic_name, owner_id)
        return topic

    async def get_topic(self, owner_id: str, topic_name: str) -> Optional[Topic]:
        return self._topics.get((owner_id, topic_name))

    async def list_user_topics(self, user_id: str) -> list[Topic]:
        """Topics ``user_id`` owns + topics ``user_id`` has been granted."""
        accessible: list[Topic] = []
        for topic_name in self._owner_index.get(user_id, set()):
            topic = self._topics.get((user_id, topic_name))
            if topic is not None:
                accessible.append(topic)
        for key in self._allowed_user_topics.get(user_id, set()):
            topic = self._topics.get(key)
            if topic is not None and topic not in accessible:
                accessible.append(topic)
        return accessible

    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        topic_names = self._owner_index.get(user_id, set())
        return [self._topics[(user_id, n)] for n in topic_names if (user_id, n) in self._topics]

    async def grant_access(self, owner_id: str, topic_name: str, user_id: str) -> bool:
        topic = self._topics.get((owner_id, topic_name))
        if not topic:
            return False
        if user_id in topic.allowed_user_ids:
            raise ValueError(f"User {user_id} already has access to topic {topic_name}")
        topic.allowed_user_ids.append(user_id)
        self._allowed_user_topics.setdefault(user_id, set()).add((owner_id, topic_name))
        logger.info("Granted access to topic %s (owner %s) for user %s", topic_name, owner_id, user_id)
        return True

    async def revoke_access(self, owner_id: str, topic_name: str, user_id: str) -> bool:
        topic = self._topics.get((owner_id, topic_name))
        if not topic:
            return False
        if user_id not in topic.allowed_user_ids:
            return False
        topic.allowed_user_ids.remove(user_id)
        self._allowed_user_topics.get(user_id, set()).discard((owner_id, topic_name))
        logger.info("Revoked access to topic %s (owner %s) for user %s", topic_name, owner_id, user_id)
        return True

    async def update_topic(
        self,
        owner_id: str,
        topic_name: str,
        is_public: Optional[bool] = None,
        description: Optional[str] = None,
    ) -> Optional[Topic]:
        topic = self._topics.get((owner_id, topic_name))
        if not topic:
            return None
        if is_public is not None:
            topic.is_public = is_public
        if description is not None:
            topic.description = description
        logger.info("Updated topic: %s (owner %s)", topic_name, owner_id)
        return topic

    async def delete_topic(self, owner_id: str, topic_name: str) -> bool:
        topic = self._topics.pop((owner_id, topic_name), None)
        if not topic:
            return False
        self._owner_index.get(owner_id, set()).discard(topic_name)
        # Tidy up the granted-access reverse index for any granted readers.
        for granted in list(topic.allowed_user_ids):
            self._allowed_user_topics.get(granted, set()).discard((owner_id, topic_name))
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
            # Auto-create semantics: caller will create the topic under
            # owner_id. The squat is closed at this layer because two
            # users' (owner, name) tuples are independent.
            return True
        if topic.owner_id == user_id:
            return True
        if permission_type != "read":
            return False
        if user_id in topic.allowed_user_ids:
            return True
        if topic.is_public:
            return True
        return False

    async def get_stats(self) -> dict[str, Any]:
        return {
            "total_topics": len(self._topics),
            "public_topics": sum(1 for t in self._topics.values() if t.is_public),
            "private_topics": sum(1 for t in self._topics.values() if not t.is_public),
        }


# ---------- Valkey ----------


# Key layout (all namespaced by owner_id):
#   topic:{owner_id}/{name}                — hash (Topic record)
#   topic:{owner_id}/{name}:allowed_users  — set of granted user_ids
#   user:{user_id}:owned_topics            — set of names this user owns
#   user:{user_id}:topics                  — set of "owner_id/name" strings
#                                             for everything the user can
#                                             access (owned + granted)
_OWNED_PREFIX = "topic:"
_ALLOWED_USERS_SUFFIX = ":allowed_users"


def _owner_topic_key(owner_id: str, topic_name: str) -> str:
    return f"{_OWNED_PREFIX}{owner_id}/{topic_name}"


def _allowed_users_key(owner_id: str, topic_name: str) -> str:
    return f"{_OWNED_PREFIX}{owner_id}/{topic_name}{_ALLOWED_USERS_SUFFIX}"


def _user_owned_key(user_id: str) -> str:
    return f"user:{user_id}:owned_topics"


def _user_topics_key(user_id: str) -> str:
    return f"user:{user_id}:topics"


def _composite_reference(owner_id: str, topic_name: str) -> str:
    """The string stored in ``user:{user_id}:topics`` for a granted
    access entry. Includes the owner so :meth:`list_user_topics` can
    look up the namespaced record."""
    return f"{owner_id}/{topic_name}"


def _split_composite_reference(ref: str) -> Optional[_TopicKey]:
    """Parse a stored ``owner_id/topic_name`` reference. Returns None
    when the value lacks the ``/`` separator — those are legacy
    flat-namespace entries that the startup migration check rejects."""
    if "/" not in ref:
        return None
    owner, name = ref.split("/", 1)
    return (owner, name)


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
            is_public=topic_data.is_public,
            description=topic_data.description,
            created_at=datetime.now(timezone.utc),
            allowed_user_ids=[],
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
                "is_public": str(topic.is_public),
                "description": topic.description or "",
                "created_at": topic.created_at.isoformat(),
            },
        )

        await self._client.sadd(_user_owned_key(owner_id), [topic.topic_name])
        await self._client.sadd(
            _user_topics_key(owner_id),
            [_composite_reference(owner_id, topic.topic_name)],
        )

        logger.info("Created topic in Valkey: %s (owner: %s)", topic.topic_name, owner_id)
        return topic

    async def get_topic(self, owner_id: str, topic_name: str) -> Optional[Topic]:
        topic_key = _owner_topic_key(owner_id, topic_name)
        topic_hash = await self._client.hgetall(topic_key)
        if not topic_hash:
            return None

        allowed_users_bytes = await self._client.smembers(_allowed_users_key(owner_id, topic_name))
        allowed_users = [u.decode("utf-8") for u in (allowed_users_bytes or [])]

        data = {k.decode("utf-8"): v.decode("utf-8") for k, v in topic_hash.items()}
        return Topic(
            topic_id=data["topic_id"],
            topic_name=data["topic_name"],
            owner_id=data["owner_id"],
            is_public=data["is_public"].lower() == "true",
            description=data.get("description") or None,
            created_at=datetime.fromisoformat(data["created_at"]),
            allowed_user_ids=allowed_users,
        )

    async def list_user_topics(self, user_id: str) -> list[Topic]:
        refs_bytes = await self._client.smembers(_user_topics_key(user_id))
        if not refs_bytes:
            return []
        topics: list[Topic] = []
        for raw in refs_bytes:
            ref = raw.decode("utf-8")
            parsed = _split_composite_reference(ref)
            if parsed is None:
                logger.warning(
                    "Legacy flat-namespace topic reference %r in user:%s:topics — skipping. "
                    "Run the migration to re-create user topic sets.",
                    ref,
                    user_id,
                )
                continue
            owner_id, name = parsed
            topic = await self.get_topic(owner_id, name)
            if topic is not None:
                topics.append(topic)
        return topics

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

    async def grant_access(self, owner_id: str, topic_name: str, user_id: str) -> bool:
        topic = await self.get_topic(owner_id, topic_name)
        if not topic:
            return False

        allowed_key = _allowed_users_key(owner_id, topic_name)
        is_member = await self._client.sismember(allowed_key, user_id)
        if is_member:
            raise ValueError(f"User {user_id} already has access to topic {topic_name}")

        await self._client.sadd(allowed_key, [user_id])
        await self._client.sadd(
            _user_topics_key(user_id),
            [_composite_reference(owner_id, topic_name)],
        )
        logger.info("Granted access to topic %s (owner %s) for user %s", topic_name, owner_id, user_id)
        return True

    async def revoke_access(self, owner_id: str, topic_name: str, user_id: str) -> bool:
        topic = await self.get_topic(owner_id, topic_name)
        if not topic:
            return False

        allowed_key = _allowed_users_key(owner_id, topic_name)
        removed = await self._client.srem(allowed_key, [user_id])
        if removed == 0:
            return False

        await self._client.srem(
            _user_topics_key(user_id),
            [_composite_reference(owner_id, topic_name)],
        )
        logger.info("Revoked access to topic %s (owner %s) for user %s", topic_name, owner_id, user_id)
        return True

    async def update_topic(
        self,
        owner_id: str,
        topic_name: str,
        is_public: Optional[bool] = None,
        description: Optional[str] = None,
    ) -> Optional[Topic]:
        topic_key = _owner_topic_key(owner_id, topic_name)
        if (await self._client.exists([topic_key])) == 0:
            return None

        updates: list[tuple[str, str]] = []
        if is_public is not None:
            updates.append(("is_public", str(is_public)))
        if description is not None:
            updates.append(("description", description))
        if updates:
            await self._client.hset(topic_key, updates)

        logger.info("Updated topic in Valkey: %s (owner %s)", topic_name, owner_id)
        return await self.get_topic(owner_id, topic_name)

    async def delete_topic(self, owner_id: str, topic_name: str) -> bool:
        topic = await self.get_topic(owner_id, topic_name)
        if not topic:
            return False

        await self._client.delete(
            [
                _owner_topic_key(owner_id, topic_name),
                _allowed_users_key(owner_id, topic_name),
            ]
        )
        await self._client.srem(_user_owned_key(owner_id), [topic_name])
        composite = _composite_reference(owner_id, topic_name)
        await self._client.srem(_user_topics_key(owner_id), [composite])
        for granted_id in topic.allowed_user_ids:
            await self._client.srem(_user_topics_key(granted_id), [composite])

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

        if topic.owner_id == user_id:
            return True
        if permission_type != "read":
            return False

        allowed_key = _allowed_users_key(owner_id, topic_name)
        is_member = await self._client.sismember(allowed_key, user_id)
        if is_member:
            return True
        if topic.is_public:
            return True
        return False

    async def get_stats(self) -> dict[str, Any]:
        # Stats require scanning all keys (expensive). Left as a stub.
        return {
            "storage_type": "valkey",
            "message": "Stats require scanning all keys (expensive operation)",
        }


async def scan_for_legacy_keys(client, *, limit: int = 100) -> list[str]:
    """Find pre-namespacing topic keys still present in Valkey.

    Returns up to ``limit`` example keys whose name portion lacks a
    ``/`` (and is therefore from the pre-Phase-3c flat namespace).
    Called by the startup migration check in ``main.py``; the relay
    refuses to boot when any are present, unless
    ``PULSAR_ALLOW_INSECURE_DEFAULTS=1``. Closes API H#5 cleanly by
    forcing an explicit migration rather than silently mixing old and
    new key shapes.
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
                # The :allowed_users suffix is the only legitimate
                # post-namespacing key that still lacks a slash in the
                # part AFTER the suffix; the owner segment before it
                # supplies the slash.
                if payload.endswith(_ALLOWED_USERS_SUFFIX):
                    payload = payload[: -len(_ALLOWED_USERS_SUFFIX)]
                if "/" not in payload:
                    examples.append(key)
                    if len(examples) >= limit:
                        return examples
            if cursor in (b"0", "0", 0):
                break
    return examples
