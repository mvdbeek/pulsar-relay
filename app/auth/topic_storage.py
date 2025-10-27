"""Topic storage backends for managing topic ownership and permissions."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from app.auth.models import Topic, TopicCreate

logger = logging.getLogger(__name__)


class TopicStorage(ABC):
    """Abstract base class for topic storage."""

    @abstractmethod
    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        """Create a new topic.

        Args:
            owner_id: User ID of the topic owner
            topic_data: Topic creation data

        Returns:
            Created topic

        Raises:
            ValueError: If topic already exists
        """
        pass

    @abstractmethod
    async def get_topic(self, topic_name: str) -> Optional[Topic]:
        """Get a topic by name.

        Args:
            topic_name: Topic name

        Returns:
            Topic if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_user_topics(self, user_id: str) -> list[Topic]:
        """List all topics accessible to a user (owned + granted access).

        Args:
            user_id: User ID

        Returns:
            List of topics
        """
        pass

    @abstractmethod
    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        """List topics owned by a user.

        Args:
            user_id: User ID

        Returns:
            List of owned topics
        """
        pass

    @abstractmethod
    async def grant_access(self, topic_name: str, user_id: str) -> bool:
        """Grant a user access to a topic.

        Args:
            topic_name: Topic name
            user_id: User ID to grant access to

        Returns:
            True if access granted, False if topic not found

        Raises:
            ValueError: If user already has access
        """
        pass

    @abstractmethod
    async def revoke_access(self, topic_name: str, user_id: str) -> bool:
        """Revoke a user's access to a topic.

        Args:
            topic_name: Topic name
            user_id: User ID to revoke access from

        Returns:
            True if access revoked, False if topic not found or user didn't have access
        """
        pass

    @abstractmethod
    async def update_topic(
        self, topic_name: str, is_public: Optional[bool] = None, description: Optional[str] = None
    ) -> Optional[Topic]:
        """Update topic metadata.

        Args:
            topic_name: Topic name
            is_public: New public status (if provided)
            description: New description (if provided)

        Returns:
            Updated topic if found, None otherwise
        """
        pass

    @abstractmethod
    async def delete_topic(self, topic_name: str) -> bool:
        """Delete a topic.

        Args:
            topic_name: Topic name

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def user_can_access(
        self, topic_name: str, user_id: str, permission_type: Literal["read", "write"], user_permissions: Sequence[str]
    ) -> bool:
        """Check if a user can access a topic.

        Args:
            topic_name: Topic name
            user_id: User ID
            permission_type: Type of access ("read" or "write")
            user_permissions: User's global permissions

        Returns:
            True if user has access, False otherwise
        """
        pass

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics.

        Returns:
            Dictionary with statistics
        """
        return {}


class InMemoryTopicStorage(TopicStorage):
    """In-memory topic storage for development/testing."""

    def __init__(self):
        """Initialize in-memory storage."""
        self._topics: dict[str, Topic] = {}  # topic_name -> Topic
        self._owner_index: dict[str, set[str]] = {}  # user_id -> set of topic_names
        logger.info("Initialized InMemoryTopicStorage")

    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        """Create a new topic."""
        # Check if topic already exists
        if topic_data.topic_name in self._topics:
            raise ValueError(f"Topic '{topic_data.topic_name}' already exists")

        # Create topic
        topic_id = str(uuid4())
        topic = Topic(
            topic_id=topic_id,
            topic_name=topic_data.topic_name,
            owner_id=owner_id,
            is_public=topic_data.is_public,
            description=topic_data.description,
            created_at=datetime.now(timezone.utc),
            allowed_user_ids=[],
        )

        # Store topic
        self._topics[topic_data.topic_name] = topic

        # Update owner index
        if owner_id not in self._owner_index:
            self._owner_index[owner_id] = set()
        self._owner_index[owner_id].add(topic_data.topic_name)

        logger.info(f"Created topic: {topic_data.topic_name} (owner: {owner_id})")
        return topic

    async def get_topic(self, topic_name: str) -> Optional[Topic]:
        """Get a topic by name."""
        return self._topics.get(topic_name)

    async def list_user_topics(self, user_id: str) -> list[Topic]:
        """List all topics accessible to a user."""
        accessible_topics = []

        for topic in self._topics.values():
            # Include if user is owner or has been granted access
            if topic.owner_id == user_id or user_id in topic.allowed_user_ids:
                accessible_topics.append(topic)

        return accessible_topics

    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        """List topics owned by a user."""
        topic_names = self._owner_index.get(user_id, set())
        return [self._topics[name] for name in topic_names if name in self._topics]

    async def grant_access(self, topic_name: str, user_id: str) -> bool:
        """Grant a user access to a topic."""
        topic = self._topics.get(topic_name)
        if not topic:
            return False

        if user_id in topic.allowed_user_ids:
            raise ValueError(f"User {user_id} already has access to topic {topic_name}")

        topic.allowed_user_ids.append(user_id)
        logger.info(f"Granted access to topic {topic_name} for user {user_id}")
        return True

    async def revoke_access(self, topic_name: str, user_id: str) -> bool:
        """Revoke a user's access to a topic."""
        topic = self._topics.get(topic_name)
        if not topic:
            return False

        if user_id not in topic.allowed_user_ids:
            return False

        topic.allowed_user_ids.remove(user_id)
        logger.info(f"Revoked access to topic {topic_name} for user {user_id}")
        return True

    async def update_topic(
        self, topic_name: str, is_public: Optional[bool] = None, description: Optional[str] = None
    ) -> Optional[Topic]:
        """Update topic metadata."""
        topic = self._topics.get(topic_name)
        if not topic:
            return None

        if is_public is not None:
            topic.is_public = is_public
        if description is not None:
            topic.description = description

        logger.info(f"Updated topic: {topic_name}")
        return topic

    async def delete_topic(self, topic_name: str) -> bool:
        """Delete a topic."""
        topic = self._topics.pop(topic_name, None)
        if not topic:
            return False

        # Remove from owner index
        if topic.owner_id in self._owner_index:
            self._owner_index[topic.owner_id].discard(topic_name)

        logger.info(f"Deleted topic: {topic_name}")
        return True

    async def user_can_access(
        self, topic_name: str, user_id: str, permission_type: Literal["read", "write"], user_permissions: Sequence[str]
    ) -> bool:
        """Check if a user can access a topic."""
        # Admin can access all topics
        if "admin" in user_permissions:
            return True

        topic = self._topics.get(topic_name)
        if not topic:
            # Topic doesn't exist - will be created on first write
            return True

        # Owner has full access
        if topic.owner_id == user_id:
            return True

        # Check if user has been granted access
        if user_id in topic.allowed_user_ids:
            return True

        # Public topics allow read access
        if permission_type == "read" and topic.is_public:
            return True

        return False

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        return {
            "total_topics": len(self._topics),
            "public_topics": sum(1 for t in self._topics.values() if t.is_public),
            "private_topics": sum(1 for t in self._topics.values() if not t.is_public),
        }


class ValkeyTopicStorage(TopicStorage):
    """Valkey-based topic storage for production use."""

    def __init__(self, client):
        """Initialize Valkey topic storage.

        Args:
            client: Connected GlideClient instance
        """
        self._client = client
        logger.info("Initialized ValkeyTopicStorage")

    def _get_topic_key(self, topic_name: str) -> str:
        """Get the Valkey key for topic data.

        Args:
            topic_name: Topic name

        Returns:
            Key in format "topic:{topic_name}"
        """
        return f"topic:{topic_name}"

    def _get_topic_allowed_users_key(self, topic_name: str) -> str:
        """Get the Valkey set key for allowed users.

        Args:
            topic_name: Topic name

        Returns:
            Key in format "topic:{topic_name}:allowed_users"
        """
        return f"topic:{topic_name}:allowed_users"

    def _get_user_owned_topics_key(self, user_id: str) -> str:
        """Get the Valkey set key for user's owned topics.

        Args:
            user_id: User ID

        Returns:
            Key in format "user:{user_id}:owned_topics"
        """
        return f"user:{user_id}:owned_topics"

    def _get_user_topics_key(self, user_id: str) -> str:
        """Get the Valkey set key for user's accessible topics.

        Args:
            user_id: User ID

        Returns:
            Key in format "user:{user_id}:topics"
        """
        return f"user:{user_id}:topics"

    async def create_topic(self, owner_id: str, topic_data: TopicCreate) -> Topic:
        """Create a new topic atomically using HSETNX."""
        topic_key = self._get_topic_key(topic_data.topic_name)

        # Create topic
        topic_id = str(uuid4())
        created_at = datetime.now(timezone.utc)
        topic = Topic(
            topic_id=topic_id,
            topic_name=topic_data.topic_name,
            owner_id=owner_id,
            is_public=topic_data.is_public,
            description=topic_data.description,
            created_at=created_at,
            allowed_user_ids=[],
        )

        # Atomically create topic using HSETNX on a sentinel field (topic_id)
        # Returns 1 if field was set (topic didn't exist), 0 if field already exists
        created = await self._client.hsetnx(topic_key, "topic_id", topic.topic_id)

        if not created:
            raise ValueError(f"Topic '{topic_data.topic_name}' already exists")

        # Topic was successfully created, now set remaining fields
        remaining_fields = {
            "topic_name": topic.topic_name,
            "owner_id": topic.owner_id,
            "is_public": str(topic.is_public),
            "description": topic.description or "",
            "created_at": topic.created_at.isoformat(),
        }
        await self._client.hset(topic_key, remaining_fields)

        # Add to user's owned topics set
        user_owned_key = self._get_user_owned_topics_key(owner_id)
        await self._client.sadd(user_owned_key, [topic_data.topic_name])

        # Add to user's accessible topics set
        user_topics_key = self._get_user_topics_key(owner_id)
        await self._client.sadd(user_topics_key, [topic_data.topic_name])

        logger.info(f"Created topic in Valkey: {topic_data.topic_name} (owner: {owner_id})")
        return topic

    async def get_topic(self, topic_name: str) -> Optional[Topic]:
        """Get a topic by name."""
        topic_key = self._get_topic_key(topic_name)

        # Get topic hash
        topic_hash = await self._client.hgetall(topic_key)
        if not topic_hash:
            return None

        # Get allowed users
        allowed_users_key = self._get_topic_allowed_users_key(topic_name)
        allowed_users_bytes = await self._client.smembers(allowed_users_key)
        allowed_users = [u.decode("utf-8") for u in (allowed_users_bytes or [])]

        # Parse topic from hash
        topic_data = {k.decode("utf-8"): v.decode("utf-8") for k, v in topic_hash.items()}

        return Topic(
            topic_id=topic_data["topic_id"],
            topic_name=topic_data["topic_name"],
            owner_id=topic_data["owner_id"],
            is_public=topic_data["is_public"].lower() == "true",
            description=topic_data.get("description") or None,
            created_at=datetime.fromisoformat(topic_data["created_at"]),
            allowed_user_ids=allowed_users,
        )

    async def list_user_topics(self, user_id: str) -> list[Topic]:
        """List all topics accessible to a user."""
        user_topics_key = self._get_user_topics_key(user_id)
        topic_names_bytes = await self._client.smembers(user_topics_key)

        if not topic_names_bytes:
            return []

        topic_names = [t.decode("utf-8") for t in topic_names_bytes]
        topics = []

        for topic_name in topic_names:
            topic = await self.get_topic(topic_name)
            if topic:
                topics.append(topic)

        return topics

    async def list_owned_topics(self, user_id: str) -> list[Topic]:
        """List topics owned by a user."""
        user_owned_key = self._get_user_owned_topics_key(user_id)
        topic_names_bytes = await self._client.smembers(user_owned_key)

        if not topic_names_bytes:
            return []

        topic_names = [t.decode("utf-8") for t in topic_names_bytes]
        topics = []

        for topic_name in topic_names:
            topic = await self.get_topic(topic_name)
            if topic:
                topics.append(topic)

        return topics

    async def grant_access(self, topic_name: str, user_id: str) -> bool:
        """Grant a user access to a topic."""
        topic = await self.get_topic(topic_name)
        if not topic:
            return False

        # Check if user already has access
        allowed_users_key = self._get_topic_allowed_users_key(topic_name)
        is_member = await self._client.sismember(allowed_users_key, user_id)

        if is_member:
            raise ValueError(f"User {user_id} already has access to topic {topic_name}")

        # Add to allowed users set
        await self._client.sadd(allowed_users_key, [user_id])

        # Add to user's accessible topics
        user_topics_key = self._get_user_topics_key(user_id)
        await self._client.sadd(user_topics_key, [topic_name])

        logger.info(f"Granted access to topic {topic_name} for user {user_id}")
        return True

    async def revoke_access(self, topic_name: str, user_id: str) -> bool:
        """Revoke a user's access to a topic."""
        topic = await self.get_topic(topic_name)
        if not topic:
            return False

        # Remove from allowed users set
        allowed_users_key = self._get_topic_allowed_users_key(topic_name)
        removed = await self._client.srem(allowed_users_key, [user_id])

        if removed == 0:
            return False

        # Remove from user's accessible topics
        user_topics_key = self._get_user_topics_key(user_id)
        await self._client.srem(user_topics_key, [topic_name])

        logger.info(f"Revoked access to topic {topic_name} for user {user_id}")
        return True

    async def update_topic(
        self, topic_name: str, is_public: Optional[bool] = None, description: Optional[str] = None
    ) -> Optional[Topic]:
        """Update topic metadata."""
        topic_key = self._get_topic_key(topic_name)

        # Check if topic exists
        exists = await self._client.exists([topic_key])
        if exists == 0:
            return None

        # Update fields
        updates = []
        if is_public is not None:
            updates.append(("is_public", str(is_public)))
        if description is not None:
            updates.append(("description", description))

        if updates:
            await self._client.hset(topic_key, updates)

        logger.info(f"Updated topic in Valkey: {topic_name}")
        return await self.get_topic(topic_name)

    async def delete_topic(self, topic_name: str) -> bool:
        """Delete a topic."""
        topic = await self.get_topic(topic_name)
        if not topic:
            return False

        topic_key = self._get_topic_key(topic_name)
        allowed_users_key = self._get_topic_allowed_users_key(topic_name)

        # Delete topic hash and allowed users set
        await self._client.delete([topic_key, allowed_users_key])

        # Remove from owner's owned topics
        user_owned_key = self._get_user_owned_topics_key(topic.owner_id)
        await self._client.srem(user_owned_key, [topic_name])

        # Remove from owner's accessible topics
        user_topics_key = self._get_user_topics_key(topic.owner_id)
        await self._client.srem(user_topics_key, [topic_name])

        # Remove from all allowed users' accessible topics
        for user_id in topic.allowed_user_ids:
            user_topics_key = self._get_user_topics_key(user_id)
            await self._client.srem(user_topics_key, [topic_name])

        logger.info(f"Deleted topic from Valkey: {topic_name}")
        return True

    async def user_can_access(
        self, topic_name: str, user_id: str, permission_type: Literal["read", "write"], user_permissions: Sequence[str]
    ) -> bool:
        """Check if a user can access a topic."""
        # Admin can access all topics
        if "admin" in user_permissions:
            return True

        topic = await self.get_topic(topic_name)
        if not topic:
            # Topic doesn't exist - will be created on first write
            return True

        # Owner has full access
        if topic.owner_id == user_id:
            return True

        # Check if user has been granted access
        allowed_users_key = self._get_topic_allowed_users_key(topic_name)
        is_member = await self._client.sismember(allowed_users_key, user_id)
        if is_member:
            return True

        # Public topics allow read access
        if permission_type == "read" and topic.is_public:
            return True

        return False

    async def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        # This would require scanning all topic keys, which is expensive
        # For now, return basic info
        return {
            "storage_type": "valkey",
            "message": "Stats require scanning all keys (expensive operation)",
        }
