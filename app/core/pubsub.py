"""Valkey Pub/Sub coordinator for cross-worker message broadcasting."""

import asyncio
import json
import logging
from collections.abc import Coroutine
from typing import Any, Callable, Optional

from glide import GlideClient, GlideClientConfiguration, NodeAddress, PubSubMsg

logger = logging.getLogger(__name__)


class PubSubCoordinator:
    """Coordinates message broadcasting across multiple worker processes using Valkey pub/sub.

    When running with multiple Uvicorn workers, each worker has its own ConnectionManager
    and PollManager. This coordinator ensures that when a message is published to a topic
    on any worker, all workers receive the message and can broadcast to their local clients.

    Uses GLIDE's native PubSub support with a dedicated subscriber client.
    """

    RELAY_CHANNEL = "relay:messages"  # Single channel for all relay messages

    def __init__(self, valkey_client: GlideClient):
        """Initialize the pub/sub coordinator.

        Args:
            valkey_client: Valkey client instance for pub/sub operations (publish)
        """
        self._publish_client = valkey_client  # Use existing client for publishing
        self._subscriber_client: Optional[GlideClient] = None
        self._message_handlers: list[Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]] = []
        self._running = False
        self._pubsub_task: Optional[asyncio.Task] = None

    def register_handler(self, handler: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Register a handler for incoming pub/sub messages.

        Args:
            handler: Async function that takes (topic, message_data) and broadcasts to local clients
        """
        self._message_handlers.append(handler)

    async def start(self) -> None:
        """Start the pub/sub subscriber in the background."""
        if self._running:
            logger.warning("PubSubCoordinator already running")
            return

        logger.info("Starting PubSubCoordinator...")
        self._running = True

        # Create a dedicated client for subscription with callback
        # GLIDE requires a separate connection for PubSub subscriptions
        try:
            config = GlideClientConfiguration(
                addresses=[
                    NodeAddress(
                        host=self._publish_client.config.addresses[0].host,
                        port=self._publish_client.config.addresses[0].port,
                    )
                ],
                use_tls=self._publish_client.config.use_tls,
                pubsub_subscriptions=GlideClientConfiguration.PubSubSubscriptions(
                    channels_and_patterns={GlideClientConfiguration.PubSubChannelModes.Exact: {self.RELAY_CHANNEL}},
                    callback=self._pubsub_callback,
                    context=None,  # Optional context passed to callback
                ),
            )

            self._subscriber_client = await GlideClient.create(config)
            logger.info(f"Subscribed to pub/sub channel: {self.RELAY_CHANNEL}")

            # Start a background task to process messages
            self._pubsub_task = asyncio.create_task(self._process_messages())

        except Exception as e:
            logger.error(f"Failed to start PubSubCoordinator: {e}")
            self._running = False
            raise

        logger.info("PubSubCoordinator started successfully")

    async def stop(self) -> None:
        """Stop the pub/sub subscriber."""
        if not self._running:
            return

        logger.info("Stopping PubSubCoordinator...")
        self._running = False

        # Cancel the processing task
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass

        # Close the subscriber client
        if self._subscriber_client:
            try:
                await self._subscriber_client.close()
            except Exception as e:
                logger.error(f"Error closing subscriber client: {e}")
            finally:
                self._subscriber_client = None

        logger.info("PubSubCoordinator stopped")

    async def publish_message(self, topic: str, message_data: dict[str, Any]) -> None:
        """Publish a message to the relay channel so all workers can broadcast it.

        Args:
            topic: Topic name
            message_data: Message data to broadcast (includes message_id, payload, timestamp, etc.)
        """
        if not self._running:
            logger.warning("PubSubCoordinator not running, skipping publish")
            return

        payload = {
            "topic": topic,
            "message": message_data,
        }

        try:
            # Publish to Valkey pub/sub channel
            # GLIDE's publish returns the number of subscribers that received the message
            num_subscribers = await self._publish_client.publish(json.dumps(payload), self.RELAY_CHANNEL)
            logger.debug(
                f"Published message to channel {self.RELAY_CHANNEL} for topic {topic} "
                f"({num_subscribers} subscribers)"
            )
        except Exception as e:
            logger.error(f"Failed to publish message to pub/sub: {e}")

    def _pubsub_callback(self, msg: PubSubMsg, context: Any) -> None:
        """Callback for incoming pub/sub messages (called by GLIDE).

        Args:
            msg: PubSubMsg from GLIDE containing message details
            context: Optional context (unused)
        """
        try:
            # Extract message content
            # msg should have attributes: channel, message, pattern
            if not msg or not hasattr(msg, "message"):
                logger.warning(f"Received invalid pub/sub message: {msg}")
                return

            message_bytes = msg.message
            if isinstance(message_bytes, bytes):
                message_str = message_bytes.decode("utf-8")
            else:
                message_str = str(message_bytes)

            # Parse JSON payload
            payload = json.loads(message_str)
            topic = payload.get("topic")
            message_data = payload.get("message")

            if not topic or not message_data:
                logger.warning(f"Invalid message payload: {payload}")
                return

            # Dispatch to all registered handlers asynchronously
            for handler in self._message_handlers:
                try:
                    # Schedule the async handler as a task
                    asyncio.create_task(handler(topic, message_data))
                except Exception as e:
                    logger.error(f"Error scheduling message handler: {e}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse pub/sub message JSON: {e}")
        except Exception as e:
            logger.error(f"Error in pub/sub callback: {e}")

    async def _process_messages(self) -> None:
        """Background task to keep the event loop alive for pub/sub processing."""
        try:
            while self._running:
                # Just keep the event loop running
                # Messages are handled by the callback
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("PubSub processing task cancelled")
        except Exception as e:
            logger.error(f"Error in pub/sub processing task: {e}")
