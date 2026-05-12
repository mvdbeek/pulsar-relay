"""WebSocket API for real-time message delivery."""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from pulsar_relay.auth.dependencies import get_topic_storage, get_user_storage
from pulsar_relay.auth.jwt import decode_token
from pulsar_relay.config import settings
from pulsar_relay.core.connections import ConnectionManager
from pulsar_relay.models import (
    WebSocketAck,
    WebSocketError,
    WebSocketPong,
    WebSocketSubscribe,
    WebSocketSubscribed,
    WebSocketUnsubscribe,
)
from pulsar_relay.utils.metrics import (
    active_websocket_connections,
    websocket_connections_total,
    websocket_disconnections_total,
)

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)

# Connection manager will be injected
_manager: Optional[ConnectionManager] = None

# Per-user concurrent WebSocket counters. Tracked in-process per worker:
# acceptable because each worker enforces its own cap and the cap is a
# defence-in-depth limit, not a hard quota. ``_per_user_lock`` guards
# the counter increment/decrement so two concurrent handshakes can't
# both observe count < cap and then push count to cap+1.
_per_user_ws_count: dict[str, int] = defaultdict(int)
_per_user_lock = asyncio.Lock()

# Subprotocol used to carry the bearer JWT across the WebSocket handshake.
# Clients offer TWO subprotocols:
#   * ``bearer`` — sentinel; the server echoes this back as the negotiated
#     subprotocol so the JWT never leaks into headers that log the
#     accepted protocol.
#   * ``bearer.<jwt>`` — the token carrier. The server extracts the JWT
#     from this offering but does NOT echo it back per RFC 6455.
# This dual-offering pattern is the one Kubernetes' API server uses.
_BEARER_SUBPROTOCOL = "bearer"
_BEARER_SUBPROTOCOL_PREFIX = "bearer."


def set_manager(manager: ConnectionManager) -> None:
    """Set the connection manager for WebSocket handling."""
    global _manager
    _manager = manager


def get_manager() -> ConnectionManager:
    """Get the current connection manager."""
    if _manager is None:
        raise RuntimeError("Connection manager not initialized")
    return _manager


def _extract_bearer_subprotocol(websocket: WebSocket) -> Optional[str]:
    """Pull the bearer JWT out of the ``Sec-WebSocket-Protocol`` header.

    Compliant clients offer two subprotocols: ``bearer`` (sentinel, which
    the server will echo) and ``bearer.<jwt>`` (token carrier, never
    echoed). The order doesn't matter. Returns ``None`` if either piece
    is missing.
    """
    raw = websocket.headers.get("sec-websocket-protocol")
    if not raw:
        return None
    offered = [chunk.strip() for chunk in raw.split(",")]
    if _BEARER_SUBPROTOCOL not in offered:
        return None
    for value in offered:
        if value.startswith(_BEARER_SUBPROTOCOL_PREFIX):
            return value[len(_BEARER_SUBPROTOCOL_PREFIX) :]
    return None


def _origin_allowed(origin: Optional[str]) -> bool:
    """Match ``Origin`` header against the configured allow-list.

    Non-browser clients (curl, wscat, server-to-server) often send no
    ``Origin`` header at all — those are accepted because the CORS
    threat model is browser-driven cross-site WebSocket hijacking.
    """
    if origin is None:
        return True
    return origin in settings.allowed_origins


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time message delivery.

    Authentication: clients pass the bearer JWT via the
    ``Sec-WebSocket-Protocol: bearer.<jwt>`` handshake header. The token
    NEVER lands in the URL, which used to leak through reverse-proxy
    access logs, browser history, and Referer headers.

    Browser clients additionally have their ``Origin`` checked against
    :data:`pulsar_relay.config.settings.allowed_origins`.

    Protocol after handshake:
    1. Client sends a subscribe message with topics.
    2. Server confirms subscription.
    3. Server pushes messages as they arrive.
    4. Client acks messages, can send pings.
    5. Client disconnects (or the server disconnects on idle timeout).
    """
    manager = get_manager()
    client_topics: list[str] = []
    session_id = f"sess_{uuid.uuid4().hex[:12]}"

    # Origin check first — cheapest reject, and a hostile cross-origin
    # browser script can't even learn whether the URL is auth-protected.
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin):
        logger.warning("WebSocket rejected: Origin %r not in allow-list", origin)
        await websocket.close(code=1008, reason="Origin not permitted")
        return

    token = _extract_bearer_subprotocol(websocket)
    if not token:
        logger.warning("WebSocket rejected: client must offer both 'bearer' and 'bearer.<jwt>' subprotocols")
        await websocket.close(code=1008, reason="Missing bearer subprotocol")
        return

    token_payload = decode_token(token)
    if token_payload is None:
        logger.warning("WebSocket connection rejected: Invalid token")
        await websocket.close(code=1008, reason="Invalid or expired token")
        return

    # Verify user exists, is active, and has read permission.
    try:
        user_storage = get_user_storage()
        user = await user_storage.get_user_by_id(token_payload.sub)
        if user is None or not user.is_active:
            logger.warning("WebSocket connection rejected: User not found or inactive")
            await websocket.close(code=1008, reason="User not found or inactive")
            return

        if "read" not in user.permissions:
            logger.warning("WebSocket connection rejected: User lacks read permission")
            await websocket.close(code=1008, reason="Permission denied: read permission required")
            return

    except Exception as e:
        logger.error(f"Error validating user for WebSocket: {e}")
        await websocket.close(code=1011, reason="Internal server error")
        return

    # Per-user concurrent-connection cap. Defends against a single
    # caller exhausting the WebSocket pool.
    async with _per_user_lock:
        if _per_user_ws_count[user.user_id] >= settings.ws_max_per_user:
            logger.warning(
                "WebSocket rejected: user %s exceeded ws_max_per_user=%d",
                user.user_id,
                settings.ws_max_per_user,
            )
            await websocket.close(code=1008, reason="Too many concurrent connections")
            return
        _per_user_ws_count[user.user_id] += 1

    accepted = False
    try:
        # Echo back the sentinel subprotocol the client offered. RFC 6455
        # requires the chosen value to be one the client offered; picking
        # the sentinel rather than the ``bearer.<jwt>`` carrier keeps the
        # JWT out of any header that logs the negotiated subprotocol.
        await websocket.accept(subprotocol=_BEARER_SUBPROTOCOL)
        accepted = True
        websocket_connections_total.inc()
        active_websocket_connections.inc()
        logger.info(f"WebSocket connection accepted: {session_id} (user: {user.username})")

        # Wait for initial subscription message — bounded by the idle
        # timeout so a connection that authenticates and then sends
        # nothing is dropped quickly.
        try:
            data = await asyncio.wait_for(websocket.receive_json(), timeout=settings.ws_idle_seconds)
            subscribe_msg = WebSocketSubscribe(**data)

            # Validate access to ALL requested topics upfront - fail early if any are denied
            topic_storage = get_topic_storage()
            denied_topics = []

            for topic in subscribe_msg.topics:
                can_access = await topic_storage.user_can_access(
                    topic_name=topic,
                    user_id=user.user_id,
                    permission_type="read",
                    user_permissions=user.permissions,
                )
                if not can_access:
                    denied_topics.append(topic)

            # Fail fast if any topics are denied
            if denied_topics:
                error = WebSocketError(
                    type="error",
                    code="SUBSCRIPTION_ERROR",
                    message="Access denied to one or more requested topics",
                )
                await websocket.send_json(error.model_dump(mode="json"))
                await websocket.close()
                return

            # All topics are allowed - subscribe to them
            client_topics = subscribe_msg.topics
            await manager.connect(websocket, client_topics)

            # Send subscription confirmation
            response = WebSocketSubscribed(
                type="subscribed",
                topics=client_topics,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc),
            )
            await websocket.send_json(response.model_dump(mode="json"))

            logger.info(f"Client {session_id} subscribed to: {client_topics}")

        except asyncio.TimeoutError:
            logger.info("Client %s timed out before subscribing", session_id)
            await websocket.close(code=1011, reason="Idle timeout before subscribe")
            return
        except Exception as e:
            logger.error("Error processing subscribe for %s: %s", session_id, e)
            error = WebSocketError(
                type="error",
                code="SUBSCRIPTION_ERROR",
                message="Failed to subscribe",
            )
            await websocket.send_json(error.model_dump(mode="json"))
            await websocket.close()
            return

        # Handle incoming messages
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=settings.ws_idle_seconds)

                # Handle different message types
                if data.get("type") == "ping":
                    # Respond to ping
                    pong = WebSocketPong(type="pong", timestamp=datetime.now(timezone.utc))
                    await websocket.send_json(pong.model_dump(mode="json"))

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
                    await websocket.send_json(error.model_dump(mode="json"))

            except asyncio.TimeoutError:
                logger.info("Client %s idle timeout, closing", session_id)
                await websocket.close(code=1011, reason="Idle timeout")
                break

            except WebSocketDisconnect:
                logger.info(f"Client {session_id} disconnected")
                break

            except Exception as e:
                # Log full detail server-side; return a generic message to
                # the client so library-internal strings don't leak.
                logger.error("Error processing WebSocket message for %s: %s", session_id, e)
                error = WebSocketError(type="error", code="PROCESSING_ERROR", message="processing error")
                try:
                    await websocket.send_json(error.model_dump(mode="json"))
                except Exception:
                    break

    except WebSocketDisconnect:
        logger.info(f"Client {session_id} disconnected during setup")

    finally:
        # Clean up connection
        async with _per_user_lock:
            _per_user_ws_count[user.user_id] -= 1
            if _per_user_ws_count[user.user_id] <= 0:
                del _per_user_ws_count[user.user_id]
        if accepted:
            await manager.disconnect(websocket)
            websocket_disconnections_total.inc()
            active_websocket_connections.dec()
            logger.info(f"Cleaned up connection for {session_id}")
