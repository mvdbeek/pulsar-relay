"""Shared test fixtures and utilities."""

import asyncio
import os
import secrets
import socket
import subprocess

# Populate startup-required secrets BEFORE pulsar_relay is imported. The
# config module calls ``load_settings()`` at import time and the lifespan's
# ``validate_startup_secrets`` enforces them. We pass real (random) values
# rather than enabling PULSAR_ALLOW_INSECURE_DEFAULTS so the test suite
# exercises the same code path as production — unless CI has explicitly
# enabled the escape hatch (its Valkey service container cannot trivially
# be made to require a password, see .github/workflows/ci.yml).
os.environ.setdefault("PULSAR_JWT_SECRET_KEY", secrets.token_urlsafe(32))
os.environ.setdefault("PULSAR_BOOTSTRAP_ADMIN_PASSWORD", secrets.token_urlsafe(16))
if os.environ.get("PULSAR_ALLOW_INSECURE_DEFAULTS") != "1":
    os.environ.setdefault("PULSAR_VALKEY_PASSWORD", secrets.token_urlsafe(16))
# CORS / TrustedHost allow-lists: tests use the FastAPI TestClient which
# sends Host=testserver and Origin=http://testserver — accept both. Real
# deployments configure these explicitly via env vars.
os.environ.setdefault("PULSAR_ALLOWED_ORIGINS", '["http://testserver", "http://test"]')
os.environ.setdefault("PULSAR_TRUSTED_HOSTS", '["testserver", "test", "localhost", "127.0.0.1"]')

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pulsar_relay.api import messages, websocket  # noqa: E402
from pulsar_relay.auth.dependencies import (  # noqa: E402
    set_device_code_storage,
    set_oidc_clients,
    set_oidc_state_storage,
    set_refresh_token_storage,
    set_topic_storage,
    set_user_storage,
)
from pulsar_relay.auth.device_flow import InMemoryDeviceCodeStorage  # noqa: E402
from pulsar_relay.auth.jwt import create_access_token  # noqa: E402
from pulsar_relay.auth.models import UserCreate  # noqa: E402
from pulsar_relay.auth.oidc_state import InMemoryOIDCStateStorage  # noqa: E402
from pulsar_relay.auth.refresh import InMemoryRefreshTokenStorage  # noqa: E402
from pulsar_relay.auth.storage import InMemoryUserStorage, UserStorage  # noqa: E402
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage  # noqa: E402
from pulsar_relay.core.connections import ConnectionManager  # noqa: E402
from pulsar_relay.core.polling import PollManager  # noqa: E402
from pulsar_relay.main import app  # noqa: E402
from pulsar_relay.storage.memory import MemoryStorage  # noqa: E402


@pytest.fixture
def anyio_backend():
    """Use asyncio backend for anyio tests."""
    return "asyncio"


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
            print(f"Created default user: {user.username} with permissions {user.permissions}")
        except ValueError as e:
            print(f"Default user already exists: {e}")


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _create_auth_storage():
    """Create a fresh user storage with default users (sync helper)."""
    storage = InMemoryUserStorage()
    _run_async(create_default_users(storage))
    return storage


@pytest.fixture
def auth_storage():
    """Create a fresh user storage with default users.

    This is a sync fixture to support both sync and async tests.
    """
    return _create_auth_storage()


@pytest.fixture
def topic_storage():
    """Create a fresh topic storage."""
    return InMemoryTopicStorage()


@pytest.fixture
def test_user(auth_storage):
    """Get a test user (with read/write permissions)."""
    return _run_async(auth_storage.get_user_by_username("user"))


@pytest.fixture
def admin_user(auth_storage):
    """Get an admin user."""
    return _run_async(auth_storage.get_user_by_username("admin"))


@pytest.fixture
def readonly_user(auth_storage):
    """Get a readonly user."""
    return _run_async(auth_storage.get_user_by_username("readonly"))


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

    # Set up refresh / device-flow / OIDC state stores so /auth/login and the
    # related endpoints can issue refresh tokens.
    set_refresh_token_storage(InMemoryRefreshTokenStorage())
    set_device_code_storage(InMemoryDeviceCodeStorage())
    set_oidc_state_storage(InMemoryOIDCStateStorage())
    set_oidc_clients({})

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
        "pulsar_relay.main:app",
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
