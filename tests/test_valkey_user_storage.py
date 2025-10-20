"""Tests for ValkeyUserStorage backend."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.auth.models import User, UserCreate
from app.auth.storage import ValkeyUserStorage


@pytest.fixture
async def valkey_user_storage():
    """Create a ValkeyUserStorage instance with mocked client."""
    mock_client = AsyncMock()
    storage = ValkeyUserStorage(client=mock_client)
    return storage


class TestValkeyUserStorage:
    """Test ValkeyUserStorage implementation."""

    @pytest.mark.asyncio
    async def test_create_user(self, valkey_user_storage):
        """Test creating a new user."""
        # Mock hget to return None (username doesn't exist in index)
        valkey_user_storage._client.hget = AsyncMock(return_value=None)
        valkey_user_storage._client.hset = AsyncMock()

        user_data = UserCreate(
            username="testuser",
            email="test@example.com",
            password="password123",
            permissions=["read", "write"],
        )

        user = await valkey_user_storage.create_user(user_data)

        # Verify user properties
        assert user.username == "testuser"
        assert user.email == "test@example.com"
        assert user.is_active is True
        assert user.permissions == ["read", "write"]
        assert user.owned_topics == []
        assert user.user_id is not None
        assert user.user_id != user.username  # UUID, not username

        # Verify hget was called to check username index
        valkey_user_storage._client.hget.assert_called_once()

        # Verify hset was called twice (once for user data, once for username index)
        assert valkey_user_storage._client.hset.call_count == 2

    @pytest.mark.asyncio
    async def test_create_user_duplicate_username(self, valkey_user_storage):
        """Test creating a user with duplicate username raises ValueError."""
        # Mock hget to return existing user_id (username exists in index)
        valkey_user_storage._client.hget = AsyncMock(return_value=b"existing-user-id-123")

        user_data = UserCreate(
            username="duplicate",
            email="duplicate@example.com",
            password="password123",
        )

        with pytest.raises(ValueError, match="Username 'duplicate' already exists"):
            await valkey_user_storage.create_user(user_data)

    @pytest.mark.asyncio
    async def test_get_user_by_id(self, valkey_user_storage):
        """Test retrieving a user by ID."""
        user_id = str(uuid4())
        created_at = datetime.now(timezone.utc)

        # Mock hgetall to return user data
        user_hash = {
            b"user_id": user_id.encode(),
            b"username": b"testuser",
            b"email": b"test@example.com",
            b"hashed_password": b"$hashed_password$",
            b"is_active": b"True",
            b"created_at": created_at.isoformat().encode(),
            b"permissions": json.dumps(["read", "write"]).encode(),
            b"owned_topics": json.dumps(["topic1"]).encode(),
        }
        valkey_user_storage._client.hgetall = AsyncMock(return_value=user_hash)

        user = await valkey_user_storage.get_user_by_id(user_id)

        # Verify user properties
        assert user is not None
        assert user.user_id == user_id
        assert user.username == "testuser"
        assert user.email == "test@example.com"
        assert user.is_active is True
        assert user.permissions == ["read", "write"]
        assert user.owned_topics == ["topic1"]

        # Verify hgetall was called with correct key
        valkey_user_storage._client.hgetall.assert_called_once_with(f"user:{user_id}")

    @pytest.mark.asyncio
    async def test_get_user_by_id_not_found(self, valkey_user_storage):
        """Test retrieving a non-existent user returns None."""
        valkey_user_storage._client.hgetall = AsyncMock(return_value=None)

        user = await valkey_user_storage.get_user_by_id("nonexistent")

        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_by_username(self, valkey_user_storage):
        """Test retrieving a user by username."""
        user_id = str(uuid4())
        created_at = datetime.now(timezone.utc)

        # Mock hget to return user_id from username index
        valkey_user_storage._client.hget = AsyncMock(return_value=user_id.encode())

        # Mock hgetall to return user data
        user_hash = {
            b"user_id": user_id.encode(),
            b"username": b"testuser",
            b"email": b"test@example.com",
            b"hashed_password": b"$hashed_password$",
            b"is_active": b"True",
            b"created_at": created_at.isoformat().encode(),
            b"permissions": json.dumps(["read"]).encode(),
            b"owned_topics": json.dumps([]).encode(),
        }
        valkey_user_storage._client.hgetall = AsyncMock(return_value=user_hash)

        user = await valkey_user_storage.get_user_by_username("testuser")

        # Verify user properties
        assert user is not None
        assert user.username == "testuser"
        assert user.user_id == user_id

        # Verify hget was called to look up user_id
        valkey_user_storage._client.hget.assert_called_once()

        # Verify hgetall was called with correct user_id
        valkey_user_storage._client.hgetall.assert_called_once_with(f"user:{user_id}")

    @pytest.mark.asyncio
    async def test_get_user_by_username_not_found(self, valkey_user_storage):
        """Test retrieving a non-existent username returns None."""
        # Mock hget to return None (username not in index)
        valkey_user_storage._client.hget = AsyncMock(return_value=None)

        user = await valkey_user_storage.get_user_by_username("nonexistent")

        assert user is None

    @pytest.mark.asyncio
    async def test_update_user(self, valkey_user_storage):
        """Test updating an existing user."""
        user_id = str(uuid4())
        user = User(
            user_id=user_id,
            username="testuser",
            email="updated@example.com",
            hashed_password="$hashed_password$",
            is_active=False,
            created_at=datetime.now(timezone.utc),
            permissions=["read"],
            owned_topics=["topic1", "topic2"],
        )

        # Mock exists to return 1 (user exists)
        valkey_user_storage._client.exists = AsyncMock(return_value=1)
        valkey_user_storage._client.hset = AsyncMock()

        updated_user = await valkey_user_storage.update_user(user)

        # Verify update returned the user
        assert updated_user.user_id == user_id
        assert updated_user.email == "updated@example.com"

        # Verify exists was called
        valkey_user_storage._client.exists.assert_called_once()

        # Verify hset was called with updated data
        valkey_user_storage._client.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_user_not_found(self, valkey_user_storage):
        """Test updating a non-existent user raises ValueError."""
        user = User(
            user_id="nonexistent",
            username="nonexistent",
            email="test@example.com",
            hashed_password="$hashed_password$",
            is_active=True,
            created_at=datetime.now(timezone.utc),
            permissions=[],
            owned_topics=[],
        )

        # Mock exists to return 0 (user doesn't exist)
        valkey_user_storage._client.exists = AsyncMock(return_value=0)

        with pytest.raises(ValueError, match="User nonexistent not found"):
            await valkey_user_storage.update_user(user)

    @pytest.mark.asyncio
    async def test_delete_user(self, valkey_user_storage):
        """Test deleting a user."""
        user_id = str(uuid4())
        created_at = datetime.now(timezone.utc)

        # Mock hgetall to return user data (for get_user_by_id call)
        user_hash = {
            b"user_id": user_id.encode(),
            b"username": b"testuser",
            b"email": b"test@example.com",
            b"hashed_password": b"$hashed_password$",
            b"is_active": b"True",
            b"created_at": created_at.isoformat().encode(),
            b"permissions": json.dumps([]).encode(),
            b"owned_topics": json.dumps([]).encode(),
        }
        valkey_user_storage._client.hgetall = AsyncMock(return_value=user_hash)
        valkey_user_storage._client.delete = AsyncMock()
        valkey_user_storage._client.hdel = AsyncMock()

        result = await valkey_user_storage.delete_user(user_id)

        # Verify delete returned True
        assert result is True

        # Verify delete and hdel were called
        valkey_user_storage._client.delete.assert_called_once_with([f"user:{user_id}"])
        valkey_user_storage._client.hdel.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_user_not_found(self, valkey_user_storage):
        """Test deleting a non-existent user returns False."""
        # Mock hgetall to return None (user doesn't exist)
        valkey_user_storage._client.hgetall = AsyncMock(return_value=None)

        result = await valkey_user_storage.delete_user("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_get_stats(self, valkey_user_storage):
        """Test getting storage statistics."""
        stats = valkey_user_storage.get_stats()

        # Verify stats format
        assert isinstance(stats, dict)
        assert stats["storage_type"] == "valkey"
        assert "message" in stats

    @pytest.mark.asyncio
    async def test_user_with_empty_email(self, valkey_user_storage):
        """Test retrieving a user with empty email."""
        user_id = str(uuid4())
        created_at = datetime.now(timezone.utc)

        # Mock hget to return user_id from username index
        valkey_user_storage._client.hget = AsyncMock(return_value=user_id.encode())

        # Mock hgetall to return user data with empty email
        user_hash = {
            b"user_id": user_id.encode(),
            b"username": b"testuser",
            b"email": b"",
            b"hashed_password": b"$hashed_password$",
            b"is_active": b"True",
            b"created_at": created_at.isoformat().encode(),
            b"permissions": json.dumps([]).encode(),
            b"owned_topics": json.dumps([]).encode(),
        }
        valkey_user_storage._client.hgetall = AsyncMock(return_value=user_hash)

        user = await valkey_user_storage.get_user_by_username("testuser")

        # Verify email is None when empty string
        assert user is not None
        assert user.email is None
