"""User storage backends."""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.auth.jwt import hash_password
from app.auth.models import User, UserCreate

logger = logging.getLogger(__name__)


class UserStorage(ABC):
    """Abstract base class for user storage."""

    @abstractmethod
    async def create_user(self, user_data: UserCreate) -> User:
        """Create a new user."""
        pass

    @abstractmethod
    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get a user by ID."""
        pass

    @abstractmethod
    async def get_user_by_username(self, username: str) -> Optional[User]:
        """Get a user by username."""
        pass

    @abstractmethod
    async def update_user(self, user: User) -> User:
        """Update an existing user."""
        pass

    @abstractmethod
    async def delete_user(self, user_id: str) -> bool:
        """Delete a user."""
        pass

    @abstractmethod
    async def list_users(self) -> list[User]:
        """List all users."""
        pass

    def get_stats(self) -> dict:
        return {}


class InMemoryUserStorage(UserStorage):
    """In-memory user storage for development/testing."""

    def __init__(self):
        """Initialize in-memory storage."""
        self._users: dict[str, User] = {}
        self._username_index: dict[str, str] = {}  # username -> user_id
        logger.info("Initialized InMemoryUserStorage")

    async def create_user(self, user_data: UserCreate) -> User:
        """Create a new user.

        Args:
            user_data: User creation data

        Returns:
            Created user

        Raises:
            ValueError: If username already exists
        """
        # Check if username already exists
        if user_data.username in self._username_index:
            raise ValueError(f"Username '{user_data.username}' already exists")

        # Create user
        user_id = str(uuid4())
        user = User(
            user_id=user_id,
            username=user_data.username,
            email=user_data.email,
            hashed_password=hash_password(user_data.password),
            is_active=True,
            created_at=datetime.now(timezone.utc),
            permissions=user_data.permissions,
        )

        # Store user
        self._users[user_id] = user
        self._username_index[user_data.username] = user_id

        logger.info(f"Created user: {user.username} ({user_id})")
        return user

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get a user by ID.

        Args:
            user_id: User ID

        Returns:
            User if found, None otherwise
        """
        return self._users.get(user_id)

    async def get_user_by_username(self, username: str) -> Optional[User]:
        """Get a user by username.

        Args:
            username: Username

        Returns:
            User if found, None otherwise
        """
        user_id = self._username_index.get(username)
        if user_id:
            return self._users.get(user_id)
        return None

    async def update_user(self, user: User) -> User:
        """Update an existing user.

        Args:
            user: User to update

        Returns:
            Updated user

        Raises:
            ValueError: If user not found
        """
        if user.user_id not in self._users:
            raise ValueError(f"User {user.user_id} not found")

        self._users[user.user_id] = user
        logger.info(f"Updated user: {user.username} ({user.user_id})")
        return user

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user.

        Args:
            user_id: User ID to delete

        Returns:
            True if deleted, False if not found
        """
        user = self._users.pop(user_id, None)
        if user:
            self._username_index.pop(user.username, None)
            logger.info(f"Deleted user: {user.username} ({user_id})")
            return True
        return False

    async def list_users(self) -> list[User]:
        """List all users.

        Returns:
            List of all users
        """
        return list(self._users.values())

    def get_stats(self) -> dict:
        """Get storage statistics.

        Returns:
            Dictionary with statistics
        """
        return {
            "total_users": len(self._users),
            "active_users": sum(1 for u in self._users.values() if u.is_active),
        }


class ValkeyUserStorage(UserStorage):
    """Valkey-based user storage for production use.

    Uses UUID for user_id with a username index for efficient lookups.
    - User data stored at: user:{user_id}
    - Username index at: user:username_index (hash mapping username -> user_id)
    """

    def __init__(self, client):
        """Initialize Valkey user storage.

        Args:
            client: Connected GlideClient instance
        """
        self._client = client
        logger.info("Initialized ValkeyUserStorage")

    def _get_user_key(self, user_id: str) -> str:
        """Get the Valkey key for user data.

        Args:
            user_id: User ID (UUID)

        Returns:
            Key in format "user:{user_id}"
        """
        return f"user:{user_id}"

    def _get_username_index_key(self) -> str:
        """Get the Valkey hash key for username to user_id mapping.

        Returns:
            Key "user:username_index"
        """
        return "user:username_index"

    async def create_user(self, user_data: UserCreate) -> User:
        """Create a new user.

        Args:
            user_data: User creation data

        Returns:
            Created user

        Raises:
            ValueError: If username already exists
        """
        username_index_key = self._get_username_index_key()

        # Generate user_id first
        user_id = str(uuid4())

        # Atomically claim the username using HSETNX
        # Returns True if field was set (username was available)
        # Returns False if field already exists (username taken)
        claimed = await self._client.hsetnx(username_index_key, user_data.username, user_id)

        if not claimed:
            raise ValueError(f"Username '{user_data.username}' already exists")

        # Username claimed successfully, now create the user
        try:
            user = User(
                user_id=user_id,
                username=user_data.username,
                email=user_data.email,
                hashed_password=hash_password(user_data.password),
                is_active=True,
                created_at=datetime.now(timezone.utc),
                permissions=user_data.permissions,
                owned_topics=[],
            )

            # Store user as hash
            user_key = self._get_user_key(user_id)
            user_hash = {
                "user_id": user.user_id,
                "username": user.username,
                "email": user.email or "",
                "hashed_password": user.hashed_password,
                "is_active": str(user.is_active),
                "created_at": user.created_at.isoformat(),
                "permissions": json.dumps(user.permissions),
                "owned_topics": json.dumps(user.owned_topics),
            }

            await self._client.hset(user_key, user_hash)

            logger.info(f"Created user in Valkey: {user.username} ({user_id})")
            return user
        except Exception as e:
            # Clean up username claim if user creation fails
            await self._client.hdel(username_index_key, [user_data.username])
            logger.error(f"Failed to create user {user_data.username}, cleaned up username claim: {e}")
            raise

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get a user by ID.

        Args:
            user_id: User ID (UUID)

        Returns:
            User if found, None otherwise
        """
        user_key = self._get_user_key(user_id)

        # Get user hash
        user_hash = await self._client.hgetall(user_key)
        if not user_hash:
            return None

        # Parse user from hash
        user_data = {k.decode("utf-8"): v.decode("utf-8") for k, v in user_hash.items()}

        return User(
            user_id=user_data["user_id"],
            username=user_data["username"],
            email=user_data.get("email") or None,
            hashed_password=user_data["hashed_password"],
            is_active=user_data["is_active"].lower() == "true",
            created_at=datetime.fromisoformat(user_data["created_at"]),
            permissions=json.loads(user_data.get("permissions", "[]")),
            owned_topics=json.loads(user_data.get("owned_topics", "[]")),
        )

    async def get_user_by_username(self, username: str) -> Optional[User]:
        """Get a user by username.

        Args:
            username: Username

        Returns:
            User if found, None otherwise
        """
        username_index_key = self._get_username_index_key()

        # Look up user_id from username index
        user_id_bytes = await self._client.hget(username_index_key, username)
        if not user_id_bytes:
            return None

        user_id = user_id_bytes.decode("utf-8")
        return await self.get_user_by_id(user_id)

    async def update_user(self, user: User) -> User:
        """Update an existing user.

        Args:
            user: User to update

        Returns:
            Updated user

        Raises:
            ValueError: If user not found
        """
        user_key = self._get_user_key(user.user_id)

        # Check if user exists
        exists = await self._client.exists([user_key])
        if exists == 0:
            raise ValueError(f"User {user.user_id} not found")

        # Update user hash
        user_hash = {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email or "",
            "hashed_password": user.hashed_password,
            "is_active": str(user.is_active),
            "created_at": user.created_at.isoformat(),
            "permissions": json.dumps(user.permissions),
            "owned_topics": json.dumps(user.owned_topics),
        }

        await self._client.hset(user_key, user_hash)

        logger.info(f"Updated user in Valkey: {user.username} ({user.user_id})")
        return user

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user.

        Args:
            user_id: User ID to delete

        Returns:
            True if deleted, False if not found
        """
        # Get user to retrieve username for index cleanup
        user = await self.get_user_by_id(user_id)
        if not user:
            return False

        user_key = self._get_user_key(user_id)
        username_index_key = self._get_username_index_key()

        # Delete user hash
        await self._client.delete([user_key])

        # Remove from username index
        await self._client.hdel(username_index_key, [user.username])

        logger.info(f"Deleted user from Valkey: {user.username} ({user_id})")
        return True

    async def list_users(self) -> list[User]:
        """List all users.

        Returns:
            List of all users
        """
        username_index_key = self._get_username_index_key()

        # Get all user_ids from the username index
        user_index = await self._client.hgetall(username_index_key)
        if not user_index:
            return []

        # Fetch all users
        users = []
        for username_bytes, user_id_bytes in user_index.items():
            user_id = user_id_bytes.decode("utf-8")
            user = await self.get_user_by_id(user_id)
            if user:
                users.append(user)

        logger.debug(f"Listed {len(users)} users from Valkey")
        return users

    def get_stats(self) -> dict:
        """Get storage statistics.

        Returns:
            Dictionary with statistics
        """
        # This would require scanning all user keys, which is expensive
        # For now, return basic info
        return {
            "storage_type": "valkey",
            "message": "Stats require scanning all keys (expensive operation)",
        }


async def create_default_users(storage: UserStorage) -> None:
    """Create default users for testing/development.

    Args:
        storage: User storage backend
    """
    default_users = [
        UserCreate(
            username="admin",
            email="admin@example.com",
            password="admin1234",
            permissions=["admin", "read", "write"],
        ),
        UserCreate(
            username="user",
            email="user@example.com",
            password="user1234",
            permissions=["read", "write"],
        ),
        UserCreate(
            username="readonly",
            email="readonly@example.com",
            password="readonly123",
            permissions=["read"],
        ),
    ]

    for user_data in default_users:
        try:
            user = await storage.create_user(user_data)
            logger.info(f"Created default user: {user.username} with permissions {user.permissions}")
        except ValueError as e:
            logger.debug(f"Default user already exists: {e}")
