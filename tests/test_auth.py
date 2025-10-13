"""Tests for authentication functionality."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import set_user_storage
from app.auth.jwt import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.auth.models import UserCreate
from app.auth.storage import InMemoryUserStorage, create_default_users
from app.main import app


@pytest.fixture
def auth_storage():
    """Create a fresh user storage for testing."""
    storage = InMemoryUserStorage()
    return storage


@pytest.fixture
def test_client():
    """Create a test client with auth storage."""
    import asyncio

    from app.api import messages
    from app.core.polling import PollManager
    from app.storage.memory import MemoryStorage

    user_storage = InMemoryUserStorage()
    msg_storage = MemoryStorage()
    poll_manager = PollManager()

    # Create default users synchronously
    loop = asyncio.get_event_loop()
    loop.run_until_complete(create_default_users(user_storage))

    # Set up app state
    set_user_storage(user_storage)
    app.state.user_storage = user_storage
    app.state.storage = msg_storage
    app.state.poll_manager = poll_manager

    # Inject dependencies
    messages.set_storage(msg_storage)
    messages.set_poll_manager(poll_manager)

    return TestClient(app)


class TestPasswordHashing:
    """Test password hashing utilities."""

    def test_hash_password(self):
        """Test password hashing."""
        password = "testpass123"
        hashed = hash_password(password)

        assert hashed != password
        assert len(hashed) > 0

    def test_verify_password(self):
        """Test password verification."""
        password = "testpass123"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True
        assert verify_password("wrongpass", hashed) is False


class TestJWTTokens:
    """Test JWT token creation and validation."""

    @pytest.mark.asyncio
    async def test_create_and_decode_token(self):
        """Test creating and decoding JWT tokens."""
        storage = InMemoryUserStorage()
        await create_default_users(storage)

        user = await storage.get_user_by_username("admin")

        # Create token
        token = create_access_token(user)
        assert len(token) > 0

        # Decode token
        payload = decode_token(token)
        assert payload is not None
        assert payload.sub == user.user_id
        assert payload.username == user.username
        assert set(payload.permissions) == set(user.permissions)

    def test_decode_invalid_token(self):
        """Test decoding an invalid token."""
        invalid_token = "invalid.token.here"
        payload = decode_token(invalid_token)
        assert payload is None

    @pytest.mark.asyncio
    async def test_token_with_custom_expiration(self):
        """Test creating token with custom expiration."""
        storage = InMemoryUserStorage()
        await create_default_users(storage)

        user = await storage.get_user_by_username("admin")

        # Create token with 1-hour expiration
        expires_delta = timedelta(hours=1)
        token = create_access_token(user, expires_delta=expires_delta)

        # Decode and verify
        payload = decode_token(token)
        assert payload is not None
        assert payload.sub == user.user_id


class TestUserStorage:
    """Test user storage operations."""

    @pytest.mark.asyncio
    async def test_create_user(self):
        """Test creating a new user."""
        storage = InMemoryUserStorage()

        user_data = UserCreate(
            username="testuser",
            email="test@example.com",
            password="testpass123",
            permissions=["read"],
        )

        user = await storage.create_user(user_data)

        assert user.username == "testuser"
        assert user.email == "test@example.com"
        assert user.is_active is True
        assert "read" in user.permissions
        assert user.hashed_password != "testpass123"

    @pytest.mark.asyncio
    async def test_create_duplicate_user(self):
        """Test that creating a duplicate username fails."""
        storage = InMemoryUserStorage()

        user_data = UserCreate(
            username="testuser",
            password="testpass123",
        )

        await storage.create_user(user_data)

        # Try to create again
        with pytest.raises(ValueError, match="already exists"):
            await storage.create_user(user_data)

    @pytest.mark.asyncio
    async def test_get_user_by_username(self):
        """Test retrieving user by username."""
        storage = InMemoryUserStorage()
        await create_default_users(storage)

        user = await storage.get_user_by_username("admin")

        assert user is not None
        assert user.username == "admin"
        assert "admin" in user.permissions

    @pytest.mark.asyncio
    async def test_get_user_by_id(self):
        """Test retrieving user by ID."""
        storage = InMemoryUserStorage()
        await create_default_users(storage)

        # First get user by username to get the ID
        user = await storage.get_user_by_username("admin")

        # Now get by ID
        user_by_id = await storage.get_user_by_id(user.user_id)

        assert user_by_id is not None
        assert user_by_id.user_id == user.user_id
        assert user_by_id.username == "admin"

    @pytest.mark.asyncio
    async def test_update_user(self):
        """Test updating a user."""
        storage = InMemoryUserStorage()
        await create_default_users(storage)

        user = await storage.get_user_by_username("user")

        # Update user
        user.email = "newemail@example.com"
        updated = await storage.update_user(user)

        assert updated.email == "newemail@example.com"

        # Verify update persisted
        fetched = await storage.get_user_by_id(user.user_id)
        assert fetched.email == "newemail@example.com"

    @pytest.mark.asyncio
    async def test_delete_user(self):
        """Test deleting a user."""
        storage = InMemoryUserStorage()
        await create_default_users(storage)

        user = await storage.get_user_by_username("readonly")

        # Delete user
        deleted = await storage.delete_user(user.user_id)
        assert deleted is True

        # Verify user is gone
        fetched = await storage.get_user_by_id(user.user_id)
        assert fetched is None


class TestAuthenticationEndpoints:
    """Test authentication HTTP endpoints."""

    def test_login_success(self, test_client):
        """Test successful login."""
        response = test_client.post(
            "/auth/login",
            json={"username": "admin", "password": "admin1234"},
        )

        assert response.status_code == 200
        data = response.json()

        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "expires_in" in data
        assert data["user"]["username"] == "admin"
        assert "admin" in data["user"]["permissions"]

    def test_login_invalid_password(self, test_client):
        """Test login with invalid password."""
        response = test_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrongpass"},
        )

        assert response.status_code == 401
        assert "Incorrect username or password" in response.json()["detail"]

    def test_login_nonexistent_user(self, test_client):
        """Test login with non-existent user."""
        response = test_client.post(
            "/auth/login",
            json={"username": "nonexistent", "password": "password"},
        )

        assert response.status_code == 401

    def test_get_current_user(self, test_client):
        """Test getting current user information."""
        # First login
        login_response = test_client.post(
            "/auth/login",
            json={"username": "user", "password": "user1234"},
        )
        token = login_response.json()["access_token"]

        # Get current user
        response = test_client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "user"
        assert "read" in data["permissions"]
        assert "write" in data["permissions"]

    def test_get_current_user_without_token(self, test_client):
        """Test accessing protected endpoint without token."""
        response = test_client.get("/auth/me")

        assert response.status_code == 403  # No auth header

    def test_get_current_user_invalid_token(self, test_client):
        """Test accessing protected endpoint with invalid token."""
        response = test_client.get(
            "/auth/me",
            headers={"Authorization": "Bearer invalid.token.here"},
        )

        assert response.status_code == 401

    def test_register_user_as_admin(self, test_client):
        """Test registering a new user as admin."""
        # Login as admin
        login_response = test_client.post(
            "/auth/login",
            json={"username": "admin", "password": "admin1234"},
        )
        token = login_response.json()["access_token"]

        # Register new user
        response = test_client.post(
            "/auth/register",
            json={
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "newpass123",
                "permissions": ["read"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "newuser"
        assert "read" in data["permissions"]

    def test_register_user_without_admin(self, test_client):
        """Test that non-admin cannot register users."""
        # Login as regular user
        login_response = test_client.post(
            "/auth/login",
            json={"username": "user", "password": "user1234"},
        )
        token = login_response.json()["access_token"]

        # Try to register new user
        response = test_client.post(
            "/auth/register",
            json={
                "username": "newuser",
                "password": "newpass123",
                "permissions": ["read"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403  # Forbidden

    def test_user_stats_as_admin(self, test_client):
        """Test getting user statistics as admin."""
        # Login as admin
        login_response = test_client.post(
            "/auth/login",
            json={"username": "admin", "password": "admin1234"},
        )
        token = login_response.json()["access_token"]

        # Get stats
        response = test_client.get(
            "/auth/users/stats",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "total_users" in data
        assert data["total_users"] >= 3  # At least the default users


class TestProtectedEndpoints:
    """Test that endpoints are properly protected."""

    def test_create_message_requires_auth(self, test_client):
        """Test that creating messages requires authentication."""
        response = test_client.post(
            "/api/v1/messages",
            json={
                "topic": "test-topic",
                "payload": {"data": "test"},
            },
        )

        # Should fail without authentication
        assert response.status_code == 403

    def test_create_message_with_auth(self, test_client):
        """Test creating message with valid authentication."""
        # Login
        login_response = test_client.post(
            "/auth/login",
            json={"username": "user", "password": "user1234"},
        )
        token = login_response.json()["access_token"]

        # Create message
        response = test_client.post(
            "/api/v1/messages",
            json={
                "topic": "test-topic",
                "payload": {"data": "test"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 201
        data = response.json()
        assert "message_id" in data

    def test_create_message_requires_write_permission(self, test_client):
        """Test that creating messages requires write permission."""
        # Login as readonly user
        login_response = test_client.post(
            "/auth/login",
            json={"username": "readonly", "password": "readonly123"},
        )
        token = login_response.json()["access_token"]

        # Try to create message
        response = test_client.post(
            "/api/v1/messages",
            json={
                "topic": "test-topic",
                "payload": {"data": "test"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        # Should fail - readonly user doesn't have write permission
        assert response.status_code == 403

    def test_poll_requires_read_permission(self, test_client):
        """Test that polling requires read permission."""
        # Login as readonly user (has read permission)
        login_response = test_client.post(
            "/auth/login",
            json={"username": "readonly", "password": "readonly123"},
        )
        token = login_response.json()["access_token"]

        # Should be able to poll
        response = test_client.post(
            "/messages/poll",
            json={
                "topics": ["test-topic"],
                "timeout": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
