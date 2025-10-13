"""Main FastAPI application."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api import auth, health, messages, polling, websocket
from app.auth.dependencies import set_user_storage
from app.auth.storage import InMemoryUserStorage, create_default_users
from app.config import settings
from app.core.connections import ConnectionManager
from app.core.polling import PollManager
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
    else:
        log.info("Using in-memory storage backend")
        storage = MemoryStorage(max_messages_per_topic=settings.max_messages_per_topic)

    # Initialize connection manager
    connection_manager = ConnectionManager()
    log.info("Initialized Connection Manager %s", connection_manager)

    # Initialize poll manager
    poll_manager = PollManager()
    log.info("Initialized Poll Manager")

    # Initialize user storage and create default users
    user_storage = InMemoryUserStorage()
    await create_default_users(user_storage)
    set_user_storage(user_storage)
    log.info("Initialized User Storage with default users")

    # Store in app state for access in routes
    app.state.storage = storage
    app.state.poll_manager = poll_manager
    app.state.user_storage = user_storage

    # Inject dependencies into API routers
    messages.set_storage(storage)
    messages.set_manager(connection_manager)
    messages.set_poll_manager(poll_manager)
    health.set_storage(storage)
    websocket.set_manager(connection_manager)

    log.info("Application startup complete")

    yield  # Application is running

    # Shutdown: Cleanup resources
    log.info("Shutting down application...")
    await storage.close()
    log.info("Application shutdown complete")


# Create FastAPI app with lifespan handler
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="High-performance message proxy with WebSocket and long-polling support",
    lifespan=lifespan,
)


# Include routers
app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["authentication"])
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
