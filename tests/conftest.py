"""Shared test fixtures and utilities."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api import messages, websocket
from app.auth.dependencies import set_topic_storage, set_user_storage
from app.auth.jwt import create_access_token
from app.auth.storage import InMemoryUserStorage, create_default_users
from app.auth.topic_storage import InMemoryTopicStorage
from app.core.connections import ConnectionManager
from app.core.polling import PollManager
from app.main import app
from app.storage.memory import MemoryStorage


@pytest.fixture
def auth_storage():
    """Create a fresh user storage with default users."""
    storage = InMemoryUserStorage()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(create_default_users(storage))
    return storage


@pytest.fixture
def topic_storage():
    """Create a fresh topic storage."""
    return InMemoryTopicStorage()


@pytest.fixture
def test_user(auth_storage):
    """Get a test user (with read/write permissions)."""
    loop = asyncio.get_event_loop()
    user = loop.run_until_complete(auth_storage.get_user_by_username("user"))
    return user


@pytest.fixture
def admin_user(auth_storage):
    """Get an admin user."""
    loop = asyncio.get_event_loop()
    user = loop.run_until_complete(auth_storage.get_user_by_username("admin"))
    return user


@pytest.fixture
def readonly_user(auth_storage):
    """Get a readonly user."""
    loop = asyncio.get_event_loop()
    user = loop.run_until_complete(auth_storage.get_user_by_username("readonly"))
    return user


@pytest.fixture
def auth_token(test_user):
    """Create a JWT token for the test user."""
    return create_access_token(test_user)


@pytest.fixture
def admin_token(admin_user):
    """Create a JWT token for the admin user."""
    return create_access_token(admin_user)


@pytest.fixture
def readonly_token(readonly_user):
    """Create a JWT token for the readonly user."""
    return create_access_token(readonly_user)


@pytest.fixture
def auth_headers(auth_token):
    """Create authorization headers with test user token."""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def admin_headers(admin_token):
    """Create authorization headers with admin token."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def readonly_headers(readonly_token):
    """Create authorization headers with readonly token."""
    return {"Authorization": f"Bearer {readonly_token}"}


@pytest.fixture
def test_client_with_auth(auth_storage, topic_storage):
    """Create a test client with full app state and authentication."""
    msg_storage = MemoryStorage()
    poll_manager = PollManager()
    conn_manager = ConnectionManager()

    # Set up authentication
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    # Set up topic storage
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage

    # Set up other app state
    app.state.storage = msg_storage
    app.state.poll_manager = poll_manager

    # Inject dependencies
    messages.set_storage(msg_storage)
    messages.set_poll_manager(poll_manager)
    messages.set_manager(conn_manager)
    websocket.set_manager(conn_manager)

    return TestClient(app)
