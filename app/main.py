"""Main FastAPI application."""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.storage.memory import MemoryStorage
from app.core.connections import ConnectionManager
from app.api import messages, health, websocket

log = logging.getLogger(__name__)

# Use uvloop for better performance (optional, requires Python <=3.12)
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass  # uvloop not available, using default event loop

# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="High-performance message proxy with WebSocket and long-polling support",
)

# Global instances
storage: MemoryStorage
connection_manager: ConnectionManager


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup."""
    global storage, connection_manager

    # Initialize storage
    storage = MemoryStorage(max_messages_per_topic=settings.max_messages_per_topic)

    # Initialize connection manager
    connection_manager = ConnectionManager()
    log.error("Initialized Connection Manager %s", connection_manager)

    # Inject dependencies into API routers
    messages.set_storage(storage)
    messages.set_manager(connection_manager)
    health.set_storage(storage)
    websocket.set_manager(connection_manager)


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    global storage

    if storage:
        await storage.close()


# Include routers
app.include_router(health.router)
app.include_router(messages.router)
app.include_router(websocket.router)

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
