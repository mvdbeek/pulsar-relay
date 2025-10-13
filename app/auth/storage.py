"""User storage backends."""

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

    def get_stats(self) -> dict:
        """Get storage statistics.

        Returns:
            Dictionary with statistics
        """
        return {
            "total_users": len(self._users),
            "active_users": sum(1 for u in self._users.values() if u.is_active),
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
