"""WebSocket API for real-time message delivery."""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.models import (
    WebSocketSubscribe,
    WebSocketUnsubscribe,
    WebSocketAck,
    WebSocketPing,
    WebSocketSubscribed,
    WebSocketPong,
    WebSocketError,
)
from app.core.connections import ConnectionManager
from app.utils.metrics import (
    websocket_connections_total,
    websocket_disconnections_total,
    active_websocket_connections,
)
from app.auth.jwt import decode_token
from app.auth.dependencies import get_user_storage

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)

# Connection manager will be injected
_manager: Optional[ConnectionManager] = None


def set_manager(manager: ConnectionManager) -> None:
    """Set the connection manager for WebSocket handling."""
    global _manager
    _manager = manager


def get_manager() -> ConnectionManager:
    """Get the current connection manager."""
    if _manager is None:
        raise RuntimeError("Connection manager not initialized")
    return _manager


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(..., description="JWT authentication token")):
    """WebSocket endpoint for real-time message delivery.

    Authentication:
    - Clients must provide a valid JWT token in the query parameter: /ws?token=<jwt_token>
    - Token is validated before accepting the connection

    Protocol:
    1. Client connects with token
    2. Client sends subscribe message with topics
    3. Server confirms subscription
    4. Server pushes messages as they arrive
    5. Client sends ack for each message
    6. Client can send ping, server responds with pong
    7. Client disconnects
    """
    manager = get_manager()
    client_topics: list[str] = []
    session_id = f"sess_{uuid.uuid4().hex[:12]}"

    # Validate token before accepting connection
    token_payload = decode_token(token)
    if token_payload is None:
        logger.warning(f"WebSocket connection rejected: Invalid token")
        await websocket.close(code=1008, reason="Invalid or expired token")
        return

    # Verify user exists and is active
    try:
        user_storage = get_user_storage()
        user = await user_storage.get_user_by_id(token_payload.sub)
        if user is None or not user.is_active:
            logger.warning(f"WebSocket connection rejected: User not found or inactive")
            await websocket.close(code=1008, reason="User not found or inactive")
            return

        # Check if user has read permission
        if "read" not in user.permissions:
            logger.warning(f"WebSocket connection rejected: User lacks read permission")
            await websocket.close(code=1008, reason="Permission denied: read permission required")
            return

    except Exception as e:
        logger.error(f"Error validating user for WebSocket: {e}")
        await websocket.close(code=1011, reason="Internal server error")
        return

    try:
        await websocket.accept()
        websocket_connections_total.inc()
        active_websocket_connections.inc()
        logger.info(f"WebSocket connection accepted: {session_id} (user: {user.username})")

        # Wait for initial subscription message
        try:
            data = await websocket.receive_json()
            subscribe_msg = WebSocketSubscribe(**data)

            # Subscribe to topics
            client_topics = subscribe_msg.topics
            await manager.connect(websocket, client_topics)

            # Send subscription confirmation
            response = WebSocketSubscribed(
                type="subscribed",
                topics=client_topics,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
            )
            await websocket.send_json(response.model_dump(mode='json'))

            logger.info(f"Client {session_id} subscribed to: {client_topics}")

        except Exception as e:
            error = WebSocketError(
                type="error",
                code="SUBSCRIPTION_ERROR",
                message=f"Failed to subscribe: {str(e)}",
            )
            await websocket.send_json(error.model_dump(mode='json'))
            await websocket.close()
            return

        # Handle incoming messages
        while True:
            try:
                data = await websocket.receive_json()

                # Handle different message types
                if data.get("type") == "ping":
                    # Respond to ping
                    pong = WebSocketPong(type="pong", timestamp=datetime.now(timezone.utc))
                    await websocket.send_json(pong.model_dump(mode='json'))

                elif data.get("type") == "ack":
                    # Acknowledge message receipt
                    ack_msg = WebSocketAck(**data)
                    logger.debug(f"Client {session_id} acknowledged: {ack_msg.message_id}")
                    # TODO: Update delivery tracking

                elif data.get("type") == "unsubscribe":
                    # Unsubscribe from topics
                    unsub_msg = WebSocketUnsubscribe(**data)
                    await manager.unsubscribe(websocket, unsub_msg.topics)

                    # Update client topics
                    for topic in unsub_msg.topics:
                        if topic in client_topics:
                            client_topics.remove(topic)

                    logger.info(f"Client {session_id} unsubscribed from: {unsub_msg.topics}")

                else:
                    # Unknown message type
                    error = WebSocketError(
                        type="error",
                        code="UNKNOWN_MESSAGE_TYPE",
                        message=f"Unknown message type: {data.get('type')}",
                    )
                    await websocket.send_json(error.model_dump(mode='json'))

            except WebSocketDisconnect:
                logger.info(f"Client {session_id} disconnected")
                break

            except Exception as e:
                logger.error(f"Error processing WebSocket message: {e}")
                error = WebSocketError(
                    type="error", code="PROCESSING_ERROR", message=str(e)
                )
                try:
                    await websocket.send_json(error.model_dump(mode='json'))
                except:
                    break

    except WebSocketDisconnect:
        logger.info(f"Client {session_id} disconnected during setup")

    finally:
        # Clean up connection
        await manager.disconnect(websocket)
        websocket_disconnections_total.inc()
        active_websocket_connections.dec()
        logger.info(f"Cleaned up connection for {session_id}")
