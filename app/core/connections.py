"""Connection manager for WebSocket connections."""

import asyncio
import logging
from sys import version_info
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and message broadcasting."""

    def __init__(self):
        """Initialize connection manager."""
        # Topic -> Set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}
        # WebSocket -> Set of subscribed topics
        self._client_topics: dict[WebSocket, set[str]] = {}
        # Lock for thread-safe operations (created lazily)
        self._lock: Optional[asyncio.Lock] = None if version_info < (3, 10) else asyncio.Lock()

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the asyncio lock.

        This is lazily initialized to avoid issues with event loop
        not being available during __init__ in Python 3.9.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def connect(self, websocket: WebSocket, topics: list[str]) -> None:
        """Connect a WebSocket to specified topics.

        Args:
            websocket: WebSocket connection
            topics: List of topic names to subscribe to
        """
        async with self._get_lock():
            # Initialize client topics if new
            if websocket not in self._client_topics:
                self._client_topics[websocket] = set()

            # Subscribe to each topic
            for topic in topics:
                if topic not in self._connections:
                    self._connections[topic] = set()

                self._connections[topic].add(websocket)
                self._client_topics[websocket].add(topic)

            logger.info(f"Client connected to topics: {topics}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Disconnect a WebSocket from all topics.

        Args:
            websocket: WebSocket connection to disconnect
        """
        async with self._get_lock():
            if websocket not in self._client_topics:
                return

            # Get topics this client was subscribed to
            topics = self._client_topics[websocket]

            # Remove from all topic subscriptions
            for topic in topics:
                if topic in self._connections:
                    self._connections[topic].discard(websocket)

                    # Clean up empty topic sets
                    if not self._connections[topic]:
                        del self._connections[topic]

            # Remove client tracking
            del self._client_topics[websocket]

            logger.info(f"Client disconnected from topics: {topics}")

    async def unsubscribe(self, websocket: WebSocket, topics: list[str]) -> None:
        """Unsubscribe a WebSocket from specified topics.

        Args:
            websocket: WebSocket connection
            topics: List of topic names to unsubscribe from
        """
        async with self._get_lock():
            if websocket not in self._client_topics:
                return

            for topic in topics:
                # Remove from topic subscriptions
                if topic in self._connections:
                    self._connections[topic].discard(websocket)

                    # Clean up empty topic sets
                    if not self._connections[topic]:
                        del self._connections[topic]

                # Remove from client topics
                self._client_topics[websocket].discard(topic)

            logger.info(f"Client unsubscribed from topics: {topics}")

    async def broadcast(self, topic: str, message: dict) -> int:
        """Broadcast a message to all connections subscribed to a topic.

        Args:
            topic: Topic name
            message: Message to broadcast

        Returns:
            Number of successful deliveries
        """
        # Take a snapshot of connections under lock to avoid race conditions
        async with self._get_lock():
            if topic not in self._connections:
                return 0
            connections = list(self._connections[topic])

        # Broadcast outside the lock to avoid blocking other operations
        dead_connections = []
        successful_deliveries = 0

        for connection in connections:
            try:
                await connection.send_json(message)
                successful_deliveries += 1
            except Exception as e:
                logger.warning(f"Failed to send message to client: {e}")
                dead_connections.append(connection)

        # Clean up dead connections
        if dead_connections:
            async with self._get_lock():
                for conn in dead_connections:
                    self._connections[topic].discard(conn)

                    # Clean up empty topic sets
                    if not self._connections[topic]:
                        del self._connections[topic]

                    # Remove from client topics
                    if conn in self._client_topics:
                        self._client_topics[conn].discard(topic)

                        # Clean up if no more topics
                        if not self._client_topics[conn]:
                            del self._client_topics[conn]

        return successful_deliveries

    async def get_connection_count(self, topic: Optional[str] = None) -> int:
        """Get the number of active connections.

        Args:
            topic: Optional topic name. If None, returns total connections.

        Returns:
            Number of connections
        """
        async with self._get_lock():
            if topic is not None:
                return len(self._connections.get(topic, set()))
            else:
                # Total unique connections across all topics
                return len(self._client_topics)

    async def get_topics_for_client(self, websocket: WebSocket) -> set[str]:
        """Get topics a client is subscribed to.

        Args:
            websocket: WebSocket connection

        Returns:
            Set of topic names
        """
        async with self._get_lock():
            return self._client_topics.get(websocket, set()).copy()

    async def get_all_topics(self) -> set[str]:
        """Get all topics with active subscriptions.

        Returns:
            Set of topic names
        """
        async with self._get_lock():
            return set(self._connections.keys())
