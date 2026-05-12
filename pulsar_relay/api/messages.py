"""Message ingestion API endpoints."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from pulsar_relay.api.limits import limiter
from pulsar_relay.auth.dependencies import get_or_create_topic, require_permission
from pulsar_relay.auth.models import User
from pulsar_relay.core.connections import ConnectionManager
from pulsar_relay.core.polling import PollManager
from pulsar_relay.core.pubsub import PubSubCoordinator
from pulsar_relay.models import (
    BulkMessageRequest,
    BulkMessageResponse,
    BulkMessageResult,
    Message,
    MessageResponse,
    WebSocketMessage,
)
from pulsar_relay.storage.base import StorageBackend
from pulsar_relay.utils.metrics import message_latency_seconds, messages_received_total

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

router = APIRouter(prefix="/api/v1", tags=["messages"])

# Dependencies will be injected
_storage: Optional[StorageBackend] = None
_manager: Optional[ConnectionManager] = None
_poll_manager: Optional[PollManager] = None
_pubsub_coordinator: Optional[PubSubCoordinator] = None


def set_storage(storage: StorageBackend) -> None:
    """Set the storage backend for the messages API."""
    global _storage
    _storage = storage


def set_manager(manager: ConnectionManager) -> None:
    """Set the connection manager for the messages API."""
    global _manager
    _manager = manager


def set_poll_manager(poll_manager: PollManager) -> None:
    """Set the poll manager for the messages API."""
    global _poll_manager
    _poll_manager = poll_manager


def set_pubsub_coordinator(pubsub_coordinator: Optional[PubSubCoordinator]) -> None:
    """Set the pub/sub coordinator for cross-worker message broadcasting."""
    global _pubsub_coordinator
    _pubsub_coordinator = pubsub_coordinator


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
@limiter.limit("120/minute")
async def create_message(
    request: Request,
    message: Message,
    current_user: User = Depends(require_permission("write")),
) -> MessageResponse:
    """Create a new message and publish to topic.

    Args:
        message: Message to create
        current_user: Current authenticated user

    Returns:
        MessageResponse with message_id and timestamp

    Raises:
        HTTPException: If user doesn't have access to the topic
    """
    storage = get_storage()

    # Ensure topic exists and user has write access
    # This will auto-create the topic if it doesn't exist, with current_user as owner
    await get_or_create_topic(message.topic, current_user)

    timestamp = datetime.now(timezone.utc)

    # Track metrics
    messages_received_total.labels(topic=message.topic).inc()

    # Save to storage - storage backend generates and returns the message ID
    with message_latency_seconds.labels(topic=message.topic).time():
        message_id = await storage.save_message(
            topic=message.topic,
            payload=message.payload,
            timestamp=timestamp,
            metadata=message.metadata,
        )

    # Prepare message for broadcasting
    ws_message = WebSocketMessage(
        type="message",
        message_id=message_id,
        topic=message.topic,
        payload=message.payload,
        timestamp=timestamp,
        metadata=message.metadata,
    )
    message_dict = ws_message.model_dump(mode="json")

    # If pub/sub coordinator is available, use it for cross-worker broadcasting
    # Otherwise, fall back to local-only broadcasting
    if _pubsub_coordinator:
        # Publish to Valkey pub/sub - all workers will receive and broadcast to their local clients
        await _pubsub_coordinator.publish_message(message.topic, message_dict)
    else:
        # Local-only broadcasting (single worker or in-memory mode)
        manager = get_manager()
        if manager:
            await manager.broadcast(message.topic, message_dict)

        if _poll_manager:
            await _poll_manager.broadcast_to_topic(message.topic, message_dict)

    return MessageResponse(message_id=message_id, topic=message.topic, timestamp=timestamp)


@router.post("/messages/bulk", response_model=BulkMessageResponse, status_code=status.HTTP_207_MULTI_STATUS)
@limiter.limit("30/minute")
async def create_bulk_messages(
    request: Request,
    payload: BulkMessageRequest,
    current_user: User = Depends(require_permission("write")),
) -> BulkMessageResponse:
    """Create multiple messages in bulk.

    Args:
        request: ASGI request (consumed by the rate limiter)
        payload: Bulk message request body
        current_user: Current authenticated user

    Returns:
        BulkMessageResponse with results for each message
    """
    storage = get_storage()

    # Validate access to ALL unique topics upfront - fail early if any are denied
    unique_topics = {msg.topic for msg in payload.messages}
    denied_topics = set()

    for topic in unique_topics:
        try:
            await get_or_create_topic(topic, current_user)
        except HTTPException:
            # Track topics that user doesn't have access to
            denied_topics.add(topic)

    # Fail fast if any topics are denied. Don't echo the topic names back
    # (Medium #11 enumeration oracle).
    if denied_topics:
        raise HTTPException(
            status_code=403,
            detail="Access denied to one or more requested topics",
        )

    # All topics validated - proceed with message creation
    results: list[BulkMessageResult] = []
    accepted = 0
    rejected = 0

    for message in payload.messages:
        try:
            timestamp = datetime.now(timezone.utc)

            # Save to storage - storage backend generates and returns the message ID
            message_id = await storage.save_message(
                topic=message.topic,
                payload=message.payload,
                timestamp=timestamp,
                metadata=message.metadata,
            )

            # Prepare message for broadcasting
            ws_message = WebSocketMessage(
                type="message",
                message_id=message_id,
                topic=message.topic,
                payload=message.payload,
                timestamp=timestamp,
                metadata=message.metadata,
            )
            message_dict = ws_message.model_dump(mode="json")

            # If pub/sub coordinator is available, use it for cross-worker broadcasting
            # Otherwise, fall back to local-only broadcasting
            if _pubsub_coordinator:
                # Publish to Valkey pub/sub - all workers will receive and broadcast to their local clients
                await _pubsub_coordinator.publish_message(message.topic, message_dict)
            else:
                # Local-only broadcasting (single worker or in-memory mode)
                manager = get_manager()
                if manager:
                    await manager.broadcast(message.topic, message_dict)

                if _poll_manager:
                    await _poll_manager.broadcast_to_topic(message.topic, message_dict)

            results.append(BulkMessageResult(message_id=message_id, topic=message.topic, status="accepted"))
            accepted += 1

        except Exception as e:
            results.append(BulkMessageResult(message_id=None, topic=message.topic, status="rejected", error=str(e)))
            rejected += 1

    return BulkMessageResponse(
        results=results,
        summary={"total": len(payload.messages), "accepted": accepted, "rejected": rejected},
    )
