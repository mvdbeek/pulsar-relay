"""Message ingestion API endpoints."""

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, status

from app.models import (
    Message,
    MessageResponse,
    BulkMessageRequest,
    BulkMessageResponse,
    BulkMessageResult,
    WebSocketMessage,
)
from app.storage.base import StorageBackend
from app.core.connections import ConnectionManager
from app.utils.metrics import messages_received_total, message_latency_seconds

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

router = APIRouter(prefix="/api/v1", tags=["messages"])

# Dependencies will be injected
_storage: Optional[StorageBackend] = None
_manager: Optional[ConnectionManager] = None


def set_storage(storage: StorageBackend) -> None:
    """Set the storage backend for the messages API."""
    global _storage
    _storage = storage


def set_manager(manager: ConnectionManager) -> None:
    """Set the connection manager for the messages API."""
    global _manager
    _manager = manager


def get_storage() -> StorageBackend:
    """Get the current storage backend."""
    if _storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend not initialized",
        )
    return _storage


def get_manager() -> Optional[ConnectionManager]:
    """Get the current connection manager (optional for testing)."""
    return _manager


@router.post("/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def create_message(message: Message) -> MessageResponse:
    """Create a new message and publish to topic.

    Args:
        message: Message to create

    Returns:
        MessageResponse with message_id and timestamp
    """
    storage = get_storage()

    # Generate unique message ID
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    timestamp = datetime.utcnow()

    # Track metrics
    messages_received_total.labels(topic=message.topic).inc()

    # Save to storage
    with message_latency_seconds.labels(topic=message.topic).time():
        await storage.save_message(
            message_id=message_id,
            topic=message.topic,
            payload=message.payload,
            timestamp=timestamp,
            metadata=message.metadata,
        )

    # Broadcast to WebSocket subscribers
    manager = get_manager()
    if manager:
        ws_message = WebSocketMessage(
            type="message",
            message_id=message_id,
            topic=message.topic,
            payload=message.payload,
            timestamp=timestamp,
            metadata=message.metadata,
        )
        await manager.broadcast(message.topic, ws_message.model_dump(mode='json'))

    return MessageResponse(message_id=message_id, topic=message.topic, timestamp=timestamp)


@router.post("/messages/bulk", response_model=BulkMessageResponse, status_code=status.HTTP_207_MULTI_STATUS)
async def create_bulk_messages(request: BulkMessageRequest) -> BulkMessageResponse:
    """Create multiple messages in bulk.

    Args:
        request: Bulk message request

    Returns:
        BulkMessageResponse with results for each message
    """
    storage = get_storage()
    results: list[BulkMessageResult] = []
    accepted = 0
    rejected = 0

    for message in request.messages:
        try:
            # Generate unique message ID
            message_id = f"msg_{uuid.uuid4().hex[:12]}"
            timestamp = datetime.utcnow()

            # Save to storage
            await storage.save_message(
                message_id=message_id,
                topic=message.topic,
                payload=message.payload,
                timestamp=timestamp,
                metadata=message.metadata,
            )

            # Broadcast to WebSocket subscribers
            manager = get_manager()
            if manager:
                ws_message = WebSocketMessage(
                    type="message",
                    message_id=message_id,
                    topic=message.topic,
                    payload=message.payload,
                    timestamp=timestamp,
                    metadata=message.metadata,
                )
                await manager.broadcast(message.topic, ws_message.model_dump(mode='json'))

            results.append(
                BulkMessageResult(
                    message_id=message_id, topic=message.topic, status="accepted"
                )
            )
            accepted += 1

        except Exception as e:
            results.append(
                BulkMessageResult(
                    message_id=None, topic=message.topic, status="rejected", error=str(e)
                )
            )
            rejected += 1

    return BulkMessageResponse(
        results=results,
        summary={"total": len(request.messages), "accepted": accepted, "rejected": rejected},
    )
