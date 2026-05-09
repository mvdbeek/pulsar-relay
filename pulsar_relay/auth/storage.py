"""User storage backends."""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pulsar_relay.auth.jwt import hash_password
from pulsar_relay.auth.models import FederatedIdentity, User, UserCreate

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

    @abstractmethod
    async def get_user_by_federated_identity(self, issuer: str, sub: str) -> Optional[User]:
        """Look up a user by their (issuer, sub) pair from an OIDC provider."""
        pass

    @abstractmethod
    async def add_federated_identity(self, user_id: str, identity: FederatedIdentity) -> User:
        """Attach a federated identity to an existing user.

        Idempotent: re-linking the same (issuer, sub) is a no-op. Raises
        ``ValueError`` if the identity is already linked to a different user.
        """
        pass

    @abstractmethod
    async def put_user(self, user: User) -> User:
        """Insert a fully-formed user, bypassing UserCreate validation.

        Used by the OIDC federation path where the user has no local
        password. Raises ``ValueError`` if the username is already taken.
        """
        pass

    def get_stats(self) -> dict:
        return {}


class InMemoryUserStorage(UserStorage):
    """In-memory user storage for development/testing."""

    def __init__(self):
        """Initialize in-memory storage."""
        self._users: dict[str, User] = {}
        self._username_index: dict[str, str] = {}  # username -> user_id
        # (issuer, sub) -> user_id, populated as federated identities are added.
        self._federated_index: dict[tuple[str, str], str] = {}
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
            for fi in user.federated_identities:
                self._federated_index.pop((fi.issuer, fi.sub), None)
            logger.info(f"Deleted user: {user.username} ({user_id})")
            return True
        return False

    async def get_user_by_federated_identity(self, issuer: str, sub: str) -> Optional[User]:
        user_id = self._federated_index.get((issuer, sub))
        if user_id is None:
            return None
        return self._users.get(user_id)

    async def put_user(self, user: User) -> User:
        if user.username in self._username_index:
            raise ValueError(f"Username '{user.username}' already exists")
        self._users[user.user_id] = user
        self._username_index[user.username] = user.user_id
        for fi in user.federated_identities:
            self._federated_index[(fi.issuer, fi.sub)] = user.user_id
        return user

    async def add_federated_identity(self, user_id: str, identity: FederatedIdentity) -> User:
        user = self._users.get(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")

        existing_owner = self._federated_index.get((identity.issuer, identity.sub))
        if existing_owner is not None and existing_owner != user_id:
            raise ValueError(f"Federated identity {identity.issuer}/{identity.sub} already linked to another user")

        # Idempotent: skip if this identity is already attached.
        already_linked = any(
            fi.issuer == identity.issuer and fi.sub == identity.sub for fi in user.federated_identities
        )
        if not already_linked:
            user.federated_identities.append(identity)
        self._federated_index[(identity.issuer, identity.sub)] = user_id
        return user

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

    def _get_federated_index_key(self) -> str:
        """Hash key mapping ``f"{issuer}|{sub}"`` to user_id."""
        return "user:fed_index"

    @staticmethod
    def _federated_index_field(issuer: str, sub: str) -> str:
        return f"{issuer}|{sub}"

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
                "hashed_password": user.hashed_password or "",
                "is_active": str(user.is_active),
                "created_at": user.created_at.isoformat(),
                "permissions": json.dumps(user.permissions),
                "owned_topics": json.dumps(user.owned_topics),
                "federated_identities": json.dumps([fi.model_dump(mode="json") for fi in user.federated_identities]),
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
            hashed_password=user_data.get("hashed_password") or None,
            is_active=user_data["is_active"].lower() == "true",
            created_at=datetime.fromisoformat(user_data["created_at"]),
            permissions=json.loads(user_data.get("permissions", "[]")),
            owned_topics=json.loads(user_data.get("owned_topics", "[]")),
            federated_identities=json.loads(user_data.get("federated_identities", "[]")),
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
            "hashed_password": user.hashed_password or "",
            "is_active": str(user.is_active),
            "created_at": user.created_at.isoformat(),
            "permissions": json.dumps(user.permissions),
            "owned_topics": json.dumps(user.owned_topics),
            "federated_identities": json.dumps([fi.model_dump(mode="json") for fi in user.federated_identities]),
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

        # Remove federated-identity index entries.
        if user.federated_identities:
            fed_index_key = self._get_federated_index_key()
            await self._client.hdel(
                fed_index_key,
                [self._federated_index_field(fi.issuer, fi.sub) for fi in user.federated_identities],
            )

        logger.info(f"Deleted user from Valkey: {user.username} ({user_id})")
        return True

    async def get_user_by_federated_identity(self, issuer: str, sub: str) -> Optional[User]:
        fed_index_key = self._get_federated_index_key()
        user_id_bytes = await self._client.hget(fed_index_key, self._federated_index_field(issuer, sub))
        if not user_id_bytes:
            return None
        return await self.get_user_by_id(user_id_bytes.decode("utf-8"))

    async def put_user(self, user: User) -> User:
        username_index_key = self._get_username_index_key()
        claimed = await self._client.hsetnx(username_index_key, user.username, user.user_id)
        if not claimed:
            raise ValueError(f"Username '{user.username}' already exists")

        user_key = self._get_user_key(user.user_id)
        user_hash = {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email or "",
            "hashed_password": user.hashed_password or "",
            "is_active": str(user.is_active),
            "created_at": user.created_at.isoformat(),
            "permissions": json.dumps(user.permissions),
            "owned_topics": json.dumps(user.owned_topics),
            "federated_identities": json.dumps([fi.model_dump(mode="json") for fi in user.federated_identities]),
        }
        await self._client.hset(user_key, user_hash)

        if user.federated_identities:
            fed_index_key = self._get_federated_index_key()
            for fi in user.federated_identities:
                await self._client.hsetnx(fed_index_key, self._federated_index_field(fi.issuer, fi.sub), user.user_id)
        return user

    async def add_federated_identity(self, user_id: str, identity: FederatedIdentity) -> User:
        user = await self.get_user_by_id(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")

        fed_index_key = self._get_federated_index_key()
        field = self._federated_index_field(identity.issuer, identity.sub)

        # Atomically claim the (issuer, sub) for this user. HSETNX returns False if
        # somebody else already holds it.
        claimed = await self._client.hsetnx(fed_index_key, field, user_id)
        if not claimed:
            existing = await self._client.hget(fed_index_key, field)
            existing_owner = existing.decode("utf-8") if existing else None
            if existing_owner != user_id:
                raise ValueError(f"Federated identity {identity.issuer}/{identity.sub} already linked to another user")

        # Idempotent: re-linking the same identity is a no-op on the user record.
        already_linked = any(
            fi.issuer == identity.issuer and fi.sub == identity.sub for fi in user.federated_identities
        )
        if not already_linked:
            user.federated_identities.append(identity)
            await self.update_user(user)
        return user

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
