"""Message ingestion API endpoints."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from pulsar_relay.api.limits import limiter
from pulsar_relay.auth.dependencies import get_or_create_topic, require_permission
from pulsar_relay.auth.models import User
from pulsar_relay.core.connections import ConnectionManager
from pulsar_relay.core.idempotency import DEFAULT_IDEMPOTENCY_TTL_SECONDS
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

    # Idempotency-Key dedupe (Client H#2). The pulsar-relay-client v1.1
    # generates one UUID per logical publish and reuses it across
    # retry attempts; we cache the response so a duplicate POST
    # returns the original message_id instead of writing again.
    owner_id = current_user.user_id
    idem_key = request.headers.get("Idempotency-Key")
    idem_storage = getattr(request.app.state, "idempotency_storage", None)
    if idem_key and idem_storage is not None:
        cached = await idem_storage.try_claim(owner_id, idem_key, DEFAULT_IDEMPOTENCY_TTL_SECONDS)
        if cached:
            log.info("Idempotency-Key hit for owner=%s key=%s — returning cached body", owner_id, idem_key)
            return MessageResponse.model_validate(cached)

    timestamp = datetime.now(timezone.utc)

    # Internal channel key is composite (owner_id, topic_name) so user
    # A's "jobs" cannot fan out to user B's "jobs" subscribers (API H#5).
    # External wire (the WebSocket subscribe topic, the Message.topic
    # field) keeps the bare name.
    channel = f"{owner_id}/{message.topic}"

    # Track metrics — metric labels intentionally use the composite key
    # so per-tenant series are distinct.
    messages_received_total.labels(topic=channel).inc()

    # Save to storage - storage backend generates and returns the message ID
    with message_latency_seconds.labels(topic=channel).time():
        message_id = await storage.save_message(
            owner_id=owner_id,
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
        await _pubsub_coordinator.publish_message(channel, message_dict)
    else:
        manager = get_manager()
        if manager:
            await manager.broadcast(channel, message_dict)
        if _poll_manager:
            await _poll_manager.broadcast_to_topic(channel, message_dict)

    response = MessageResponse(message_id=message_id, topic=message.topic, timestamp=timestamp)
    if idem_key and idem_storage is not None:
        await idem_storage.record(owner_id, idem_key, response.model_dump(mode="json"), DEFAULT_IDEMPOTENCY_TTL_SECONDS)
    return response


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

    # Idempotency-Key dedupe (Client H#2) — see create_message above
    # for the rationale. One header applies to the whole bulk submit;
    # a retried bulk POST returns the original 207 body verbatim.
    owner_id = current_user.user_id
    idem_key = request.headers.get("Idempotency-Key")
    idem_storage = getattr(request.app.state, "idempotency_storage", None)
    if idem_key and idem_storage is not None:
        cached = await idem_storage.try_claim(owner_id, idem_key, DEFAULT_IDEMPOTENCY_TTL_SECONDS)
        if cached:
            log.info("Bulk Idempotency-Key hit for owner=%s key=%s — returning cached body", owner_id, idem_key)
            return BulkMessageResponse.model_validate(cached)

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
            channel = f"{owner_id}/{message.topic}"

            message_id = await storage.save_message(
                owner_id=owner_id,
                topic=message.topic,
                payload=message.payload,
                timestamp=timestamp,
                metadata=message.metadata,
            )

            ws_message = WebSocketMessage(
                type="message",
                message_id=message_id,
                topic=message.topic,
                payload=message.payload,
                timestamp=timestamp,
                metadata=message.metadata,
            )
            message_dict = ws_message.model_dump(mode="json")

            if _pubsub_coordinator:
                await _pubsub_coordinator.publish_message(channel, message_dict)
            else:
                manager = get_manager()
                if manager:
                    await manager.broadcast(channel, message_dict)
                if _poll_manager:
                    await _poll_manager.broadcast_to_topic(channel, message_dict)

            results.append(BulkMessageResult(message_id=message_id, topic=message.topic, status="accepted"))
            accepted += 1

        except Exception as e:
            results.append(BulkMessageResult(message_id=None, topic=message.topic, status="rejected", error=str(e)))
            rejected += 1

    bulk_response = BulkMessageResponse(
        results=results,
        summary={"total": len(payload.messages), "accepted": accepted, "rejected": rejected},
    )
    if idem_key and idem_storage is not None:
        await idem_storage.record(
            owner_id, idem_key, bulk_response.model_dump(mode="json"), DEFAULT_IDEMPOTENCY_TTL_SECONDS
        )
    return bulk_response
