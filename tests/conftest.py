"""Shared test fixtures and utilities."""

import asyncio
import os
import socket
import subprocess

import httpx
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
async def auth_storage():
    """Create a fresh user storage with default users."""
    storage = InMemoryUserStorage()
    await create_default_users(storage)
    return storage


@pytest.fixture
def topic_storage():
    """Create a fresh topic storage."""
    return InMemoryTopicStorage()


@pytest.fixture
async def test_user(auth_storage):
    """Get a test user (with read/write permissions)."""
    user = await auth_storage.get_user_by_username("user")
    return user


@pytest.fixture
async def admin_user(auth_storage):
    """Get an admin user."""
    user = await auth_storage.get_user_by_username("admin")
    return user


@pytest.fixture
async def readonly_user(auth_storage):
    """Get a readonly user."""
    user = await auth_storage.get_user_by_username("readonly")
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


@pytest.fixture
async def real_server(request):
    """Spin up a real pulsar-relay server instance for integration testing.

    This fixture is useful for testing scenarios that require multiple concurrent
    WebSocket connections or other integration testing that can't be done with
    the standard test client due to event loop isolation issues.

    The server is started with a bootstrap admin user configured via environment
    variables, and a free port is automatically allocated to avoid conflicts.

    Supports parameterization via pytest.mark.parametrize or indirect parameters:
        - workers: Number of uvicorn workers (default: 1)
        - storage_backend: "memory" or "valkey" (default: "memory")
        - valkey_host: Valkey host (default: "localhost")
        - valkey_port: Valkey port (default: 6379)

    Example usage:
        # Single worker with memory storage (default)
        async def test_single_worker(real_server):
            base_url = real_server["base_url"]

        # Multiple workers with Valkey (using indirect param)
        @pytest.mark.parametrize("real_server", [
            {"workers": 3, "storage_backend": "valkey"}
        ], indirect=True)
        async def test_multiworker(real_server):
            assert real_server["workers"] == 3

    Yields:
        dict: Server configuration with keys:
            - base_url: HTTP base URL (e.g., "http://127.0.0.1:12345")
            - ws_url: WebSocket base URL (e.g., "ws://127.0.0.1:12345")
            - username: Bootstrap admin username
            - password: Bootstrap admin password
            - email: Bootstrap admin email
            - workers: Number of workers started
            - storage_backend: Storage backend in use
    """
    # Get parameters from request (if using indirect parametrize) or use defaults
    params = getattr(request, "param", {})
    workers = params.get("workers", 1)
    storage_backend = params.get("storage_backend", "memory")
    valkey_host = params.get("valkey_host", "localhost")
    valkey_port = params.get("valkey_port", 6379)

    def find_free_port():
        """Find an available port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}"

    # Bootstrap admin credentials
    username = "testuser"
    password = "testpass123"
    email = "test@example.com"

    # Build environment variables
    env = {
        **os.environ,
        "PULSAR_BOOTSTRAP_ADMIN_USERNAME": username,
        "PULSAR_BOOTSTRAP_ADMIN_PASSWORD": password,
        "PULSAR_BOOTSTRAP_ADMIN_EMAIL": email,
        "PULSAR_STORAGE_BACKEND": storage_backend,
    }

    # Add Valkey config if using valkey backend
    if storage_backend == "valkey":
        env["PULSAR_VALKEY_HOST"] = str(valkey_host)
        env["PULSAR_VALKEY_PORT"] = str(valkey_port)
        env["PULSAR_LOG_LEVEL"] = "INFO"

    # Build uvicorn command
    cmd = [
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]

    # Add workers if > 1
    if workers > 1:
        cmd.extend(["--workers", str(workers)])

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        # Wait for server to start - longer wait for multiple workers
        startup_time = 3 if workers == 1 else 5
        await asyncio.sleep(startup_time)

        # Verify server is responding
        max_attempts = 20 if workers > 1 else 10
        async with httpx.AsyncClient() as client:
            for attempt in range(max_attempts):
                try:
                    health_response = await client.get(f"{base_url}/health")
                    if health_response.status_code == 200:
                        break
                except Exception:
                    if attempt == max_attempts - 1:
                        raise Exception(f"Server failed to start after {max_attempts} attempts")
                await asyncio.sleep(0.5)

        yield {
            "base_url": base_url,
            "ws_url": ws_url,
            "username": username,
            "password": password,
            "email": email,
            "workers": workers,
            "storage_backend": storage_backend,
        }

    finally:
        # Terminate the server
        process.terminate()
        try:
            timeout = 10 if workers > 1 else 5
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
