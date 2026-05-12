"""Pydantic models for request/response validation."""

from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import AfterValidator, BaseModel, Field


def _validate_topic_charset(value: str) -> str:
    """Reject topic names with characters that would collide with Valkey
    key separators or be visually confusable.

    Allowed: ``[A-Za-z0-9_-]``, 1..255 chars. Rejected: ``:``, ``/``,
    whitespace, control chars, ``*``, etc. Centralising the check here
    means a hostile topic name like ``"foo:allowed_users"`` cannot land
    in storage key construction from any input path (REST, WS, poll).
    """
    if not value:
        raise ValueError("Topic name must not be empty")
    if len(value) > 255:
        raise ValueError("Topic name must be at most 255 characters")
    # Same charset previously enforced only on Message.topic; now applied
    # everywhere via the TopicName alias.
    if not value.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Topic must contain only alphanumeric characters, hyphens, and underscores")
    return value


TopicName = Annotated[str, AfterValidator(_validate_topic_charset)]
"""Validated topic name. Use in every model/path that accepts a topic
name so the charset and length constraints are enforced uniformly.
Closes API M#10 (charset only enforced on POST /messages previously)."""


class Message(BaseModel):
    """Message model for incoming messages from producers."""

    topic: TopicName = Field(..., description="Topic name")
    payload: dict[str, Any] = Field(..., description="Message payload as JSON")
    ttl: Optional[int] = Field(None, gt=0, description="Time-to-live in seconds")
    metadata: Optional[dict[str, str]] = Field(None, description="Optional metadata")

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
    topics: list[TopicName] = Field(..., min_length=1, max_length=50)
    client_id: str = Field(..., min_length=1, max_length=255)
    offset: str = Field("last", description="'last', 'earliest', or specific message_id")


class WebSocketUnsubscribe(BaseModel):
    """WebSocket unsubscribe message."""

    type: str = Field("unsubscribe", pattern="^unsubscribe$")
    topics: list[TopicName] = Field(..., min_length=1)


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


class StoredMessage(BaseModel):
    """Stored message model for GET operations."""

    message_id: str = Field(..., description="Unique message identifier")
    topic: str = Field(..., description="Topic name")
    payload: dict[str, Any] = Field(..., description="Message payload")
    timestamp: str = Field(..., description="Message timestamp")
    metadata: Optional[dict[str, str]] = Field(None, description="Optional metadata")


class PaginatedMessagesResponse(BaseModel):
    """Paginated messages response."""

    messages: list[StoredMessage] = Field(..., description="List of messages")
    total: int = Field(..., description="Total number of messages returned")
    limit: int = Field(..., description="Requested limit")
    order: str = Field(..., description="Order of messages: 'asc' (oldest first) or 'desc' (newest first)")
    cursor: Optional[str] = Field(None, description="Cursor message ID (if provided)")
    next_cursor: Optional[str] = Field(None, description="Message ID to use for next page")
