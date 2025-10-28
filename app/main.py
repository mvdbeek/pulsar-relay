"""Main FastAPI application."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api import auth, health, messages, polling, topics, websocket
from app.auth.dependencies import set_topic_storage, set_user_storage
from app.auth.jwt import hash_password
from app.auth.models import UserCreate
from app.auth.storage import InMemoryUserStorage, UserStorage, ValkeyUserStorage
from app.auth.topic_storage import InMemoryTopicStorage, TopicStorage, ValkeyTopicStorage
from app.config import settings
from app.core.connections import ConnectionManager
from app.core.polling import PollManager
from app.core.pubsub import PubSubCoordinator
from app.storage.memory import MemoryStorage
from app.storage.valkey import ValkeyStorage

log = logging.getLogger(__name__)

# Use uvloop for better performance (optional, requires Python <=3.12)
try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass  # uvloop not available, using default event loop


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan events (startup and shutdown)."""
    # Startup: Initialize all services
    log.info("Starting up application...")

    # Initialize storage based on configuration
    if settings.storage_backend == "valkey":
        log.info("Using Valkey storage backend")
        storage: Union[ValkeyStorage, MemoryStorage] = ValkeyStorage(
            host=settings.valkey_host,
            port=settings.valkey_port,
            max_messages_per_topic=settings.max_messages_per_topic,
            ttl_seconds=settings.persistent_tier_retention,
            use_tls=settings.valkey_use_tls,
        )
        assert isinstance(storage, ValkeyStorage)
        # Connect to Valkey
        await storage.connect()
        log.info(f"Connected to Valkey at {settings.valkey_host}:{settings.valkey_port}")
        topic_storage: TopicStorage = ValkeyTopicStorage(storage._client)
        log.info("Initialized Valkey Topic Storage")
        user_storage: UserStorage = ValkeyUserStorage(storage._client)
        log.info("Initialized Valkey User Storage")
    else:
        log.info("Using in-memory storage backend")
        storage = MemoryStorage(max_messages_per_topic=settings.max_messages_per_topic)
        # Initialize user storage
        user_storage = InMemoryUserStorage()
        log.info("Initialized In-Memory User Storage")
        topic_storage = InMemoryTopicStorage()
        log.info("Initialized In-Memory Topic Storage")

    # Initialize connection manager
    connection_manager = ConnectionManager()
    log.info("Initialized Connection Manager %s", connection_manager)

    # Initialize poll manager
    poll_manager = PollManager()
    log.info("Initialized Poll Manager")

    # Initialize pub/sub coordinator for multi-worker message broadcasting
    # Only enable if using Valkey backend (required for cross-worker coordination)
    pubsub_coordinator = None
    if settings.storage_backend == "valkey" and isinstance(storage, ValkeyStorage):
        pubsub_coordinator = PubSubCoordinator(storage._client)

        # Register handlers to broadcast messages to local clients
        async def handle_pubsub_message(topic: str, message_data: dict) -> None:
            """Handle incoming pub/sub messages and broadcast to local clients."""
            # Broadcast to WebSocket clients on this worker
            await connection_manager.broadcast(topic, message_data)

            # Broadcast to long-polling clients on this worker
            await poll_manager.broadcast_to_topic(topic, message_data)

        pubsub_coordinator.register_handler(handle_pubsub_message)
        await pubsub_coordinator.start()
        log.info("Initialized PubSub Coordinator for multi-worker broadcasting")

    set_user_storage(user_storage)
    set_topic_storage(topic_storage)

    # Bootstrap admin user if configured
    if settings.bootstrap_admin_username and settings.bootstrap_admin_password:
        try:
            # Check if admin already exists
            existing_admin = await user_storage.get_user_by_username(settings.bootstrap_admin_username)
            if not existing_admin:
                admin_data = UserCreate(
                    username=settings.bootstrap_admin_username,
                    password=settings.bootstrap_admin_password,
                    email=settings.bootstrap_admin_email or f"{settings.bootstrap_admin_username}@example.com",
                    permissions=["admin", "read", "write"],
                )
                admin_user = await user_storage.create_user(admin_data)
                log.info(f"✅ Bootstrap admin created: {admin_user.username}")
            else:
                log.info(f"Bootstrap admin already exists: {settings.bootstrap_admin_username}")
                hashed_password = hash_password(settings.bootstrap_admin_password)
                if hashed_password != existing_admin.hashed_password:
                    existing_admin.hashed_password = hashed_password
                    await user_storage.update_user(existing_admin)
        except Exception as e:
            log.error(f"Failed to create bootstrap admin: {e}")

    # Store in app state for access in routes
    app.state.storage = storage
    app.state.poll_manager = poll_manager
    app.state.user_storage = user_storage
    app.state.topic_storage = topic_storage
    app.state.pubsub_coordinator = pubsub_coordinator

    # Inject dependencies into API routers
    messages.set_storage(storage)
    messages.set_manager(connection_manager)
    messages.set_poll_manager(poll_manager)
    messages.set_pubsub_coordinator(pubsub_coordinator)
    health.set_storage(storage)
    websocket.set_manager(connection_manager)

    log.info("Application startup complete")

    yield  # Application is running

    # Shutdown: Cleanup resources
    log.info("Shutting down application...")
    if pubsub_coordinator:
        await pubsub_coordinator.stop()
    await storage.close()
    log.info("Application shutdown complete")


# Create FastAPI app with lifespan handler
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="High-performance message relay with WebSocket and long-polling support",
    lifespan=lifespan,
)


# Include routers
app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["authentication"])
app.include_router(topics.router)
app.include_router(messages.router)
app.include_router(websocket.router)
app.include_router(polling.router, prefix="/messages", tags=["polling"])

# Initialize Prometheus metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler."""
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "An internal error occurred",
            "details": str(exc) if settings.log_level == "DEBUG" else None,
        },
    )
