"""Long polling HTTP endpoint as WebSocket fallback."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.polling import PollManager
from app.storage.base import StorageBackend

logger = logging.getLogger(__name__)

router = APIRouter()


class PollRequest(BaseModel):
    """Request model for long polling."""

    topics: List[str] = Field(..., description="Topics to subscribe to", min_length=1)
    since: Optional[Dict[str, str]] = Field(
        default=None,
        description="Last message ID seen per topic for catching up on missed messages",
    )
    timeout: int = Field(
        default=30,
        description="Maximum seconds to wait for new messages",
        ge=1,
        le=60,
    )


class PollResponse(BaseModel):
    """Response model for long polling."""

    messages: List[Dict[str, Any]] = Field(
        default_factory=list, description="Messages received"
    )
    has_more: bool = Field(
        default=False,
        description="Whether there might be more messages available immediately",
    )


@router.post("/poll", response_model=PollResponse)
async def long_poll(
    poll_request: PollRequest,
    request: Request,
) -> PollResponse:
    """Long polling endpoint for receiving messages.

    This endpoint allows clients to receive messages without WebSocket support.
    The server will hold the connection open until messages arrive or timeout.

    Args:
        poll_request: Polling request with topics and parameters
        request: FastAPI request object for accessing app state

    Returns:
        PollResponse with any messages received

    Raises:
        HTTPException: If storage is unavailable or invalid parameters
    """
    # Get dependencies from app state
    poll_manager: PollManager = request.app.state.poll_manager
    storage: StorageBackend = request.app.state.storage

    # Validate topics
    if not poll_request.topics:
        raise HTTPException(status_code=400, detail="At least one topic required")

    messages = []

    try:
        # First, check for any recent messages the client hasn't seen
        if poll_request.since:
            # Client wants to catch up on messages
            for topic in poll_request.topics:
                since_id = poll_request.since.get(topic)
                try:
                    recent_messages = await storage.get_messages(
                        topic, since=since_id, limit=100
                    )
                    for msg in recent_messages:
                        messages.append(
                            {
                                "topic": topic,
                                "message_id": msg["message_id"],
                                "payload": msg["payload"],
                                "timestamp": msg["timestamp"],
                                "metadata": msg.get("metadata", {}),
                                "stream_id": msg.get("stream_id"),
                            }
                        )
                except Exception as e:
                    logger.error(f"Error fetching messages for topic {topic}: {e}")

        # If we already have messages, return immediately
        if messages:
            logger.debug(
                f"Returning {len(messages)} cached messages immediately"
            )
            return PollResponse(messages=messages, has_more=len(messages) >= 100)

        # No cached messages, create waiter for new messages
        waiter = await poll_manager.create_waiter(poll_request.topics)

        try:
            # Wait for new messages with timeout
            logger.debug(
                f"Waiting for new messages on topics {poll_request.topics} "
                f"with timeout {poll_request.timeout}s"
            )
            new_messages = await waiter.wait_for_messages(
                timeout=float(poll_request.timeout)
            )

            if new_messages:
                logger.info(
                    f"Returning {len(new_messages)} new messages to poll client"
                )
                messages.extend(new_messages)

        finally:
            # Always clean up the waiter
            await poll_manager.remove_waiter(waiter.client_id)

        return PollResponse(messages=messages, has_more=False)

    except Exception as e:
        logger.error(f"Error in long polling: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/poll/stats")
async def get_poll_stats(request: Request) -> Dict[str, Any]:
    """Get statistics about active long polling clients.

    Args:
        request: FastAPI request object

    Returns:
        Statistics about poll manager
    """
    poll_manager: PollManager = request.app.state.poll_manager
    return poll_manager.get_stats()
