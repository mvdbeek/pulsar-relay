"""Prometheus metrics for monitoring."""

from prometheus_client import Counter, Gauge, Histogram

# Message metrics
messages_received_total = Counter(
    "proxy_messages_received_total",
    "Total number of messages received",
    ["topic"],
)

messages_delivered_total = Counter(
    "proxy_messages_delivered_total",
    "Total number of messages delivered",
    ["topic", "delivery_type"],
)

message_latency_seconds = Histogram(
    "proxy_message_latency_seconds",
    "Message delivery latency in seconds",
    ["topic"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0),
)

# Connection metrics
active_websocket_connections = Gauge(
    "proxy_websocket_connections_active",
    "Number of active WebSocket connections",
)

websocket_connections_total = Counter(
    "proxy_websocket_connections_total",
    "Total number of WebSocket connections established",
)

websocket_disconnections_total = Counter(
    "proxy_websocket_disconnections_total",
    "Total number of WebSocket disconnections",
)

# Storage metrics
storage_operations_total = Counter(
    "proxy_storage_operations_total",
    "Total number of storage operations",
    ["operation", "status"],
)

topic_message_count = Gauge(
    "proxy_topic_message_count",
    "Number of messages in each topic",
    ["topic"],
)

# Error metrics
errors_total = Counter(
    "proxy_errors_total",
    "Total number of errors",
    ["type", "code"],
)
