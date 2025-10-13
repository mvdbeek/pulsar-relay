"""Health and readiness check endpoints."""

import datetime
from typing import Optional

from fastapi import APIRouter

from app.models import HealthResponse, ReadinessResponse
from app.storage.base import StorageBackend

router = APIRouter(tags=["health"])

# Storage backend will be injected
_storage: Optional[StorageBackend] = None


def set_storage(storage: StorageBackend) -> None:
    """Set the storage backend for health checks."""
    global _storage
    _storage = storage


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Basic health check endpoint."""
    return HealthResponse(status="healthy", timestamp=datetime.datetime.now(datetime.UTC))


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check() -> ReadinessResponse:
    """Readiness check with dependency status."""
    checks = {}

    # Check storage
    if _storage is not None:
        try:
            storage_healthy = await _storage.health_check()
            checks["storage"] = "ok" if storage_healthy else "unhealthy"
        except Exception as e:
            checks["storage"] = f"error: {str(e)}"
    else:
        checks["storage"] = "not_initialized"

    # Overall readiness
    ready = all(status == "ok" for status in checks.values())

    return ReadinessResponse(ready=ready, checks=checks)
