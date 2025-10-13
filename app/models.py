"""Pydantic models for request/response validation."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class Message(BaseModel):
    """Message model for incoming messages from producers."""

    topic: str = Field(..., min_length=1, max_length=255, description="Topic name")
    payload: dict[str, Any] = Field(..., description="Message payload as JSON")
    ttl: Optional[int] = Field(None, gt=0, description="Time-to-live in seconds")
    metadata: Optional[dict[str, str]] = Field(None, description="Optional metadata")

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        """Validate topic name format."""
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Topic must contain only alphanumeric characters, hyphens, and underscores")
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "topic": "notifications",
                    "payload": {"user_id": 123, "message": "Hello, World!"},
                    "ttl": 3600,
                    "metadata": {"priority": "high", "correlation_id": "abc-123"},
                }
            ]
        }
    }


class MessageResponse(BaseModel):
    """Response model for message creation."""

    message_id: str = Field(..., description="Unique message identifier")
    topic: str = Field(..., description="Topic name")
    timestamp: datetime = Field(..., description="Message timestamp")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "message_id": "msg_abc123def456",
                    "topic": "notifications",
                    "timestamp": "2025-10-09T12:00:00Z",
                }
            ]
        }
    }


class BulkMessageRequest(BaseModel):
    """Request model for bulk message submission."""

    messages: list[Message] = Field(..., min_length=1, max_length=100)


class BulkMessageResult(BaseModel):
    """Result for a single message in bulk request."""

    message_id: Optional[str] = None
    topic: str
    status: str  # "accepted" or "rejected"
    error: Optional[str] = None


class BulkMessageResponse(BaseModel):
    """Response model for bulk message submission."""

    results: list[BulkMessageResult]
    summary: dict[str, int] = Field(default_factory=lambda: {"total": 0, "accepted": 0, "rejected": 0})


class WebSocketSubscribe(BaseModel):
    """WebSocket subscription message."""

    type: str = Field("subscribe", pattern="^subscribe$")
    topics: list[str] = Field(..., min_length=1, max_length=50)
    client_id: str = Field(..., min_length=1, max_length=255)
    offset: str = Field("last", description="'last', 'earliest', or specific message_id")


class WebSocketUnsubscribe(BaseModel):
    """WebSocket unsubscribe message."""

    type: str = Field("unsubscribe", pattern="^unsubscribe$")
    topics: list[str] = Field(..., min_length=1)


class WebSocketAck(BaseModel):
    """WebSocket acknowledgment message."""

    type: str = Field("ack", pattern="^ack$")
    message_id: str


class WebSocketPing(BaseModel):
    """WebSocket ping message."""

    type: str = Field("ping", pattern="^ping$")


class WebSocketMessage(BaseModel):
    """WebSocket message delivery."""

    type: str = "message"
    message_id: str
    topic: str
    payload: dict[str, Any]
    timestamp: datetime
    metadata: Optional[dict[str, str]] = None


class WebSocketPong(BaseModel):
    """WebSocket pong response."""

    type: str = "pong"
    timestamp: datetime


class WebSocketSubscribed(BaseModel):
    """WebSocket subscription confirmation."""

    type: str = "subscribed"
    topics: list[str]
    session_id: str
    timestamp: datetime


class WebSocketError(BaseModel):
    """WebSocket error message."""

    type: str = "error"
    code: str
    message: str
    details: Optional[dict[str, Any]] = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: datetime
    version: str = "0.1.0"


class ReadinessResponse(BaseModel):
    """Readiness check response."""

    ready: bool
    checks: dict[str, str]
