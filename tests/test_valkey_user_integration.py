"""Integration tests for ValkeyUserStorage backend.

These tests require a running Valkey instance on localhost:6379.
Start Valkey with: docker run -d -p 6379:6379 valkey/valkey:latest

Run these tests with: VALKEY_INTEGRATION_TEST=1 pytest tests/test_valkey_user_integration.py -v
"""

import asyncio
import os
from datetime import datetime, timezone

import pytest
from glide import GlideClient, GlideClientConfiguration, NodeAddress

from app.auth.models import User, UserCreate
from app.auth.storage import ValkeyUserStorage

pytestmark = pytest.mark.skipif(
    not os.getenv("VALKEY_INTEGRATION_TEST"), reason="VALKEY_INTEGRATION_TEST environment variable not set"
)


@pytest.fixture
async def valkey_client():
    """Create a connected Valkey client."""
    config = GlideClientConfiguration(
        addresses=[NodeAddress(host="localhost", port=6379)],
        use_tls=False,
        request_timeout=5000,
    )
    client = await GlideClient.create(config)
    yield client
    await client.close()


@pytest.fixture
async def valkey_user_storage(valkey_client):
    """Create a ValkeyUserStorage instance with a real Valkey connection."""
    storage = ValkeyUserStorage(client=valkey_client)

    # Clear any existing test users
    await valkey_client.flushall()

    yield storage

    # Cleanup after tests
    try:
        await valkey_client.flushall()
    except Exception:
        pass


class TestValkeyUserStorageIntegrationBasics:
    """Basic integration tests for ValkeyUserStorage."""

    @pytest.mark.asyncio
    async def test_create_and_retrieve_user_by_username(self, valkey_user_storage):
        """Test creating and retrieving a user by username."""
        user_data = UserCreate(
            username="testuser",
            email="test@example.com",
            password="password123",
            permissions=["read", "write"],
        )

        # Create user
        created_user = await valkey_user_storage.create_user(user_data)

        # Verify created user properties
        assert created_user.username == "testuser"
        assert created_user.user_id is not None
        assert created_user.user_id != "testuser"  # user_id is UUID, not username
        assert created_user.email == "test@example.com"
        assert created_user.is_active is True
        assert created_user.permissions == ["read", "write"]
        assert created_user.owned_topics == []

        # Retrieve by username
        retrieved_user = await valkey_user_storage.get_user_by_username("testuser")

        # Verify retrieved user matches
        assert retrieved_user is not None
        assert retrieved_user.username == created_user.username
        assert retrieved_user.user_id == created_user.user_id
        assert retrieved_user.email == created_user.email
        assert retrieved_user.hashed_password == created_user.hashed_password
        assert retrieved_user.is_active == created_user.is_active
        assert retrieved_user.permissions == created_user.permissions

    @pytest.mark.asyncio
    async def test_create_and_retrieve_user_by_id(self, valkey_user_storage):
        """Test creating and retrieving a user by ID (which is username)."""
        user_data = UserCreate(
            username="testuser2",
            email="test2@example.com",
            password="password456",
            permissions=["admin"],
        )

        # Create user
        created_user = await valkey_user_storage.create_user(user_data)

        # Retrieve by user_id (UUID)
        retrieved_user = await valkey_user_storage.get_user_by_id(created_user.user_id)

        # Verify retrieved user matches
        assert retrieved_user is not None
        assert retrieved_user.username == "testuser2"
        assert retrieved_user.user_id == created_user.user_id
        assert retrieved_user.email == "test2@example.com"
        assert retrieved_user.permissions == ["admin"]

    @pytest.mark.asyncio
    async def test_create_duplicate_username(self, valkey_user_storage):
        """Test that creating a duplicate username raises ValueError."""
        user_data = UserCreate(
            username="duplicate",
            email="user1@example.com",
            password="password123",
        )

        # Create first user
        await valkey_user_storage.create_user(user_data)

        # Try to create duplicate
        duplicate_data = UserCreate(
            username="duplicate",
            email="user2@example.com",
            password="different_password",
        )

        with pytest.raises(ValueError, match="Username 'duplicate' already exists"):
            await valkey_user_storage.create_user(duplicate_data)

    @pytest.mark.asyncio
    async def test_get_nonexistent_user(self, valkey_user_storage):
        """Test retrieving a non-existent user returns None."""
        user = await valkey_user_storage.get_user_by_username("nonexistent")
        assert user is None

        user_by_id = await valkey_user_storage.get_user_by_id("nonexistent")
        assert user_by_id is None

    @pytest.mark.asyncio
    async def test_create_user_without_email(self, valkey_user_storage):
        """Test creating a user without an email."""
        user_data = UserCreate(
            username="noemail",
            email=None,
            password="password123",
            permissions=["read"],
        )

        created_user = await valkey_user_storage.create_user(user_data)
        assert created_user.email is None

        # Retrieve and verify
        retrieved_user = await valkey_user_storage.get_user_by_username("noemail")
        assert retrieved_user is not None
        assert retrieved_user.email is None


class TestValkeyUserStorageIntegrationUpdates:
    """Test user update operations."""

    @pytest.mark.asyncio
    async def test_update_user(self, valkey_user_storage):
        """Test updating an existing user."""
        # Create user
        user_data = UserCreate(
            username="updatetest",
            email="before@example.com",
            password="password123",
            permissions=["read"],
        )
        created_user = await valkey_user_storage.create_user(user_data)

        # Modify user
        created_user.email = "after@example.com"
        created_user.permissions = ["read", "write", "admin"]
        created_user.is_active = False

        # Update user
        updated_user = await valkey_user_storage.update_user(created_user)

        # Verify update returned correct data
        assert updated_user.email == "after@example.com"
        assert updated_user.permissions == ["read", "write", "admin"]
        assert updated_user.is_active is False

        # Retrieve and verify changes persisted
        retrieved_user = await valkey_user_storage.get_user_by_username("updatetest")
        assert retrieved_user is not None
        assert retrieved_user.email == "after@example.com"
        assert retrieved_user.permissions == ["read", "write", "admin"]
        assert retrieved_user.is_active is False

    @pytest.mark.asyncio
    async def test_update_nonexistent_user(self, valkey_user_storage):
        """Test updating a non-existent user raises ValueError."""
        user = User(
            user_id="nonexistent",
            username="nonexistent",
            email="test@example.com",
            hashed_password="$hashed$",
            is_active=True,
            created_at=datetime.now(timezone.utc),
            permissions=[],
            owned_topics=[],
        )

        with pytest.raises(ValueError, match="User nonexistent not found"):
            await valkey_user_storage.update_user(user)

    @pytest.mark.asyncio
    async def test_update_user_owned_topics(self, valkey_user_storage):
        """Test updating user's owned topics."""
        # Create user
        user_data = UserCreate(
            username="topicowner",
            email="owner@example.com",
            password="password123",
        )
        created_user = await valkey_user_storage.create_user(user_data)
        assert created_user.owned_topics == []

        # Add owned topics
        created_user.owned_topics = ["topic1", "topic2", "topic3"]
        await valkey_user_storage.update_user(created_user)

        # Verify topics persisted
        retrieved_user = await valkey_user_storage.get_user_by_username("topicowner")
        assert retrieved_user.owned_topics == ["topic1", "topic2", "topic3"]


class TestValkeyUserStorageIntegrationDeletion:
    """Test user deletion operations."""

    @pytest.mark.asyncio
    async def test_delete_user(self, valkey_user_storage):
        """Test deleting a user."""
        # Create user
        user_data = UserCreate(
            username="deletetest",
            email="delete@example.com",
            password="password123",
        )
        created_user = await valkey_user_storage.create_user(user_data)

        # Verify user exists
        user = await valkey_user_storage.get_user_by_username("deletetest")
        assert user is not None

        # Delete user (by user_id)
        result = await valkey_user_storage.delete_user(created_user.user_id)
        assert result is True

        # Verify user no longer exists
        deleted_user = await valkey_user_storage.get_user_by_username("deletetest")
        assert deleted_user is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_user(self, valkey_user_storage):
        """Test deleting a non-existent user returns False."""
        result = await valkey_user_storage.delete_user("doesnotexist")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_and_recreate_user(self, valkey_user_storage):
        """Test deleting and recreating a user with the same username."""
        # Create user
        user_data = UserCreate(
            username="recreate",
            email="first@example.com",
            password="password1",
            permissions=["read"],
        )
        first_user = await valkey_user_storage.create_user(user_data)

        # Delete user (by user_id)
        await valkey_user_storage.delete_user(first_user.user_id)

        # Recreate with different data
        new_user_data = UserCreate(
            username="recreate",
            email="second@example.com",
            password="password2",
            permissions=["write"],
        )
        second_user = await valkey_user_storage.create_user(new_user_data)

        # Verify new user has different data
        assert second_user.email == "second@example.com"
        assert second_user.permissions == ["write"]
        assert second_user.hashed_password != first_user.hashed_password
        assert second_user.user_id != first_user.user_id  # Different UUIDs


class TestValkeyUserStorageIntegrationMultipleUsers:
    """Test operations with multiple users."""

    @pytest.mark.asyncio
    async def test_create_multiple_users(self, valkey_user_storage):
        """Test creating multiple users."""
        users = []
        for i in range(10):
            user_data = UserCreate(
                username=f"user{i}",
                email=f"user{i}@example.com",
                password=f"password{i}",
                permissions=["read"] if i % 2 == 0 else ["write"],
            )
            user = await valkey_user_storage.create_user(user_data)
            users.append(user)

        # Verify all users can be retrieved
        for i in range(10):
            retrieved = await valkey_user_storage.get_user_by_username(f"user{i}")
            assert retrieved is not None
            assert retrieved.email == f"user{i}@example.com"
            assert retrieved.permissions == (["read"] if i % 2 == 0 else ["write"])

    @pytest.mark.asyncio
    async def test_concurrent_user_creation(self, valkey_user_storage):
        """Test that usernames remain unique even with concurrent operations."""

        async def create_user(index: int):
            user_data = UserCreate(
                username=f"concurrent{index}",
                email=f"concurrent{index}@example.com",
                password=f"password{index}",
            )
            return await valkey_user_storage.create_user(user_data)

        # Create 20 users concurrently
        users = await asyncio.gather(*[create_user(i) for i in range(20)])

        # Verify all users were created
        assert len(users) == 20

        # Verify all usernames are unique
        usernames = [u.username for u in users]
        assert len(set(usernames)) == 20

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_username_creation(self, valkey_user_storage):
        """Test that only one user is created when multiple concurrent requests try to create the same username.

        This test specifically validates the race condition fix using HSETNX for atomic username claims.
        """

        async def create_admin_user():
            user_data = UserCreate(
                username="admin",
                email="admin@example.com",
                password="admin123",
                permissions=["admin", "read", "write"],
            )
            return await valkey_user_storage.create_user(user_data)

        # Try to create the same user 10 times concurrently
        results = await asyncio.gather(*[create_admin_user() for _ in range(10)], return_exceptions=True)

        # Count successes and failures (create_user returns User, not UserCreate)
        successes = [r for r in results if isinstance(r, User)]
        failures = [r for r in results if isinstance(r, ValueError)]

        # Exactly one should succeed
        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}. Results: {results}"
        # The rest should fail with ValueError
        assert len(failures) == 9, f"Expected 9 failures, got {len(failures)}. Failures: {failures}"

        # Verify all failures have the correct error message
        for failure in failures:
            assert "already exists" in str(failure)

        # Verify the successful user was created
        admin_user = successes[0]
        assert admin_user.username == "admin"
        assert admin_user.permissions == ["admin", "read", "write"]

        # Verify we can retrieve the user
        retrieved = await valkey_user_storage.get_user_by_username("admin")
        assert retrieved is not None
        assert retrieved.user_id == admin_user.user_id

        # Verify there's only one unique user_id among all successful results
        user_ids = [r.user_id for r in results if isinstance(r, User)]
        assert len(set(user_ids)) == 1, f"Expected all admin users to have same ID, got {set(user_ids)}"


class TestValkeyUserStorageIntegrationEdgeCases:
    """Test edge cases and special characters."""

    @pytest.mark.asyncio
    async def test_username_with_special_characters(self, valkey_user_storage):
        """Test usernames with various special characters."""
        special_usernames = [
            "user-with-dash",
            "user_with_underscore",
            "user.with.dots",
            "user@with@at",
        ]

        for username in special_usernames:
            user_data = UserCreate(
                username=username,
                email=f"{username}@example.com",
                password="password123",
            )
            created_user = await valkey_user_storage.create_user(user_data)
            assert created_user.username == username

            # Verify retrieval works
            retrieved = await valkey_user_storage.get_user_by_username(username)
            assert retrieved is not None
            assert retrieved.username == username

    @pytest.mark.asyncio
    async def test_unicode_in_email(self, valkey_user_storage):
        """Test handling Unicode characters in email."""
        user_data = UserCreate(
            username="unicode_user",
            email="用户@例え.com",
            password="password123",
        )

        created_user = await valkey_user_storage.create_user(user_data)
        assert created_user.email == "用户@例え.com"

        # Retrieve and verify
        retrieved = await valkey_user_storage.get_user_by_username("unicode_user")
        assert retrieved is not None
        assert retrieved.email == "用户@例え.com"

    @pytest.mark.asyncio
    async def test_valid_permissions(self, valkey_user_storage):
        """Test handling all valid permission types."""
        # Only admin, read, and write are valid permissions
        valid_permissions = ["admin", "read", "write"]

        user_data = UserCreate(
            username="many_perms",
            email="perms@example.com",
            password="password123",
            permissions=valid_permissions,
        )

        created_user = await valkey_user_storage.create_user(user_data)
        assert len(created_user.permissions) == 3
        assert set(created_user.permissions) == set(valid_permissions)

        # Retrieve and verify
        retrieved = await valkey_user_storage.get_user_by_username("many_perms")
        assert retrieved is not None
        assert len(retrieved.permissions) == 3
        assert set(retrieved.permissions) == set(valid_permissions)


class TestValkeyUserStorageIntegrationStats:
    """Test storage statistics."""

    @pytest.mark.asyncio
    async def test_get_stats(self, valkey_user_storage):
        """Test getting storage statistics."""
        stats = valkey_user_storage.get_stats()

        assert isinstance(stats, dict)
        assert stats["storage_type"] == "valkey"
        assert "message" in stats


@pytest.mark.asyncio
async def test_full_workflow():
    """Test a complete workflow: connect, create, read, update, delete."""
    # Create client
    config = GlideClientConfiguration(
        addresses=[NodeAddress(host="localhost", port=6379)],
        use_tls=False,
        request_timeout=5000,
    )
    client = await GlideClient.create(config)

    try:
        # Clear any existing data
        await client.flushall()

        # Create storage
        storage = ValkeyUserStorage(client=client)

        # Create user
        user_data = UserCreate(
            username="workflow_user",
            email="workflow@example.com",
            password="password123",
            permissions=["read", "write"],
        )
        created_user = await storage.create_user(user_data)
        assert created_user.username == "workflow_user"

        # Read by username
        user = await storage.get_user_by_username("workflow_user")
        assert user is not None
        assert user.email == "workflow@example.com"

        # Read by ID (using the UUID from created_user)
        user_by_id = await storage.get_user_by_id(created_user.user_id)
        assert user_by_id is not None
        assert user_by_id.email == "workflow@example.com"

        # Update
        user.email = "updated@example.com"
        user.permissions = ["admin"]
        updated = await storage.update_user(user)
        assert updated.email == "updated@example.com"
        assert updated.permissions == ["admin"]

        # Verify update persisted
        user_after_update = await storage.get_user_by_username("workflow_user")
        assert user_after_update.email == "updated@example.com"
        assert user_after_update.permissions == ["admin"]

        # Delete (using user_id)
        deleted = await storage.delete_user(created_user.user_id)
        assert deleted is True

        # Verify deletion
        deleted_user = await storage.get_user_by_username("workflow_user")
        assert deleted_user is None

        # Clear
        await client.flushall()

    finally:
        # Cleanup
        await client.close()
