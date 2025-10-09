# Pulsar Proxy API Reference

Complete API reference for the Pulsar Proxy message delivery system.

## Base URL

```
http://localhost:8080/api/v1
```

## Authentication

All API requests require authentication using one of the following methods:

### API Key (Producers)

```http
Authorization: Bearer YOUR_API_KEY
```

### JWT Token (Consumers)

```http
Authorization: Bearer YOUR_JWT_TOKEN
```

## Producer API

### Send Message

Send a single message to a topic.

**Endpoint:** `POST /api/v1/messages`

**Request:**

```json
{
  "topic": "string (required)",
  "payload": "object (required)",
  "ttl": "integer (optional, seconds)",
  "metadata": {
    "key": "value"
  }
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| topic | string | Yes | Topic name to publish to |
| payload | object | Yes | Message payload (any valid JSON) |
| ttl | integer | No | Time-to-live in seconds (default: topic retention) |
| metadata | object | No | Additional metadata key-value pairs |

**Response:** `201 Created`

```json
{
  "message_id": "msg_abc123",
  "topic": "notifications",
  "timestamp": "2025-10-09T12:00:00Z"
}
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 400 | INVALID_REQUEST | Malformed request body |
| 401 | UNAUTHORIZED | Invalid or missing API key |
| 403 | FORBIDDEN | Not authorized for this topic |
| 413 | PAYLOAD_TOO_LARGE | Message exceeds size limit |
| 429 | RATE_LIMIT_EXCEEDED | Rate limit exceeded |
| 500 | INTERNAL_ERROR | Server error |

**Example:**

```bash
curl -X POST http://localhost:8080/api/v1/messages \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "notifications",
    "payload": {
      "user_id": 123,
      "message": "Your order has shipped!",
      "order_id": "ORD-456"
    },
    "ttl": 3600,
    "metadata": {
      "priority": "high",
      "correlation_id": "corr-789"
    }
  }'
```

---

### Send Bulk Messages

Send multiple messages in a single request.

**Endpoint:** `POST /api/v1/messages/bulk`

**Request:**

```json
{
  "messages": [
    {
      "topic": "string",
      "payload": "object",
      "ttl": "integer",
      "metadata": {}
    }
  ]
}
```

**Response:** `207 Multi-Status`

```json
{
  "results": [
    {
      "message_id": "msg_123",
      "topic": "topic1",
      "status": "accepted"
    },
    {
      "topic": "topic2",
      "status": "rejected",
      "error": "INVALID_TOPIC",
      "message": "Topic does not exist"
    }
  ],
  "summary": {
    "total": 2,
    "accepted": 1,
    "rejected": 1
  }
}
```

**Example:**

```bash
curl -X POST http://localhost:8080/api/v1/messages/bulk \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {
        "topic": "notifications",
        "payload": {"user_id": 1, "message": "Hello"}
      },
      {
        "topic": "alerts",
        "payload": {"user_id": 2, "message": "Warning"}
      }
    ]
  }'
```

---

## Consumer API (Long-Polling)

### Poll for Messages

Wait for new messages on subscribed topics.

**Endpoint:** `GET /api/v1/poll`

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| topics | string | Yes | Comma-separated list of topics |
| timeout | integer | No | Max wait time in seconds (default: 30, max: 60) |
| since | string | No | Last received message_id (for resuming) |
| limit | integer | No | Max messages to return (default: 10, max: 100) |

**Response:** `200 OK` (messages available)

```json
{
  "messages": [
    {
      "message_id": "msg_124",
      "topic": "notifications",
      "payload": {
        "user_id": 123,
        "message": "Hello"
      },
      "timestamp": "2025-10-09T12:00:00Z",
      "metadata": {
        "priority": "high"
      }
    }
  ],
  "next_offset": "msg_124",
  "has_more": true
}
```

**Response:** `304 Not Modified` (no messages)

```json
{
  "messages": [],
  "next_offset": "msg_123",
  "timeout": true
}
```

**Example:**

```bash
curl "http://localhost:8080/api/v1/poll?topics=notifications,alerts&timeout=30&since=msg_100" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

### Acknowledge Messages

Acknowledge receipt of messages.

**Endpoint:** `POST /api/v1/ack`

**Request:**

```json
{
  "message_ids": ["msg_123", "msg_124", "msg_125"]
}
```

**Response:** `200 OK`

```json
{
  "acknowledged": 3,
  "failed": []
}
```

**Example:**

```bash
curl -X POST http://localhost:8080/api/v1/ack \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message_ids": ["msg_123", "msg_124"]
  }'
```

---

## WebSocket API

### Connection

**Endpoint:** `ws://localhost:8080/ws?token=YOUR_TOKEN`

**Protocol:** WebSocket (RFC 6455)

### Message Format

All WebSocket messages are JSON-formatted.

#### Client → Server Messages

##### Subscribe

Subscribe to one or more topics.

```json
{
  "type": "subscribe",
  "topics": ["notifications", "alerts"],
  "client_id": "client-123",
  "offset": "last"
}
```

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| type | string | Yes | Must be "subscribe" |
| topics | array | Yes | List of topic names |
| client_id | string | Yes | Unique client identifier |
| offset | string | No | "last", "earliest", or specific message_id |

**Response:**

```json
{
  "type": "subscribed",
  "topics": ["notifications", "alerts"],
  "session_id": "sess_xyz789",
  "timestamp": "2025-10-09T12:00:00Z"
}
```

---

##### Unsubscribe

Unsubscribe from topics.

```json
{
  "type": "unsubscribe",
  "topics": ["alerts"]
}
```

**Response:**

```json
{
  "type": "unsubscribed",
  "topics": ["alerts"]
}
```

---

##### Acknowledge

Acknowledge receipt of a message.

```json
{
  "type": "ack",
  "message_id": "msg_123"
}
```

**Response:** (none - fire and forget)

---

##### Ping

Send heartbeat to keep connection alive.

```json
{
  "type": "ping"
}
```

**Response:**

```json
{
  "type": "pong",
  "timestamp": "2025-10-09T12:00:00Z"
}
```

---

#### Server → Client Messages

##### Message

Deliver a message to the client.

```json
{
  "type": "message",
  "message_id": "msg_123",
  "topic": "notifications",
  "payload": {
    "user_id": 123,
    "message": "Hello"
  },
  "timestamp": "2025-10-09T12:00:00Z",
  "metadata": {
    "priority": "high"
  }
}
```

---

##### Error

Notify client of an error.

```json
{
  "type": "error",
  "code": "TOPIC_NOT_FOUND",
  "message": "Topic 'xyz' does not exist",
  "details": {}
}
```

**Error Codes:**

| Code | Description |
|------|-------------|
| INVALID_MESSAGE | Malformed message format |
| TOPIC_NOT_FOUND | Requested topic does not exist |
| UNAUTHORIZED | Authentication failed |
| RATE_LIMIT_EXCEEDED | Too many requests |
| CONNECTION_CLOSED | Server is closing connection |

---

##### Pong

Response to ping.

```json
{
  "type": "pong",
  "timestamp": "2025-10-09T12:00:00Z"
}
```

---

### Connection Lifecycle

1. **Connect**: Client opens WebSocket connection with auth token
2. **Subscribe**: Client sends subscribe message
3. **Receive**: Server pushes messages as they arrive
4. **Acknowledge**: Client acknowledges each message
5. **Heartbeat**: Ping/pong every 30 seconds
6. **Disconnect**: Either side can close gracefully

### Reconnection Strategy

If connection is lost:

1. Wait with exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (max)
2. Reconnect with auth token
3. Subscribe to same topics
4. Provide last received message_id in offset field
5. Server resumes delivery from that point

**Example:**

```javascript
const ws = new WebSocket('ws://localhost:8080/ws?token=YOUR_TOKEN');

ws.onopen = () => {
  ws.send(JSON.stringify({
    type: 'subscribe',
    topics: ['notifications'],
    client_id: 'client-123',
    offset: lastMessageId || 'last'
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'subscribed':
      console.log('Subscribed to:', data.topics);
      break;

    case 'message':
      console.log('Received message:', data.payload);
      lastMessageId = data.message_id;

      // Acknowledge
      ws.send(JSON.stringify({
        type: 'ack',
        message_id: data.message_id
      }));
      break;

    case 'error':
      console.error('Error:', data.message);
      break;
  }
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Disconnected, reconnecting...');
  setTimeout(connect, 1000);
};

// Heartbeat
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}, 30000);
```

---

## Management API

### Health Check

Check if the service is healthy.

**Endpoint:** `GET /health`

**Response:** `200 OK`

```json
{
  "status": "healthy",
  "timestamp": "2025-10-09T12:00:00Z",
  "version": "1.0.0"
}
```

---

### Readiness Check

Check if the service is ready to accept traffic.

**Endpoint:** `GET /ready`

**Response:** `200 OK` (ready) or `503 Service Unavailable` (not ready)

```json
{
  "ready": true,
  "checks": {
    "redis": "ok",
    "postgresql": "ok",
    "queue": "ok"
  }
}
```

---

### Metrics

Prometheus metrics endpoint.

**Endpoint:** `GET /metrics`

**Response:** `200 OK` (Prometheus text format)

```
# HELP proxy_connections_total Active connections by type
# TYPE proxy_connections_total gauge
proxy_connections_total{type="websocket"} 1234
proxy_connections_total{type="longpoll"} 567

# HELP proxy_messages_received_total Total messages received
# TYPE proxy_messages_received_total counter
proxy_messages_received_total{topic="notifications"} 45678

# HELP proxy_messages_delivered_total Total messages delivered
# TYPE proxy_messages_delivered_total counter
proxy_messages_delivered_total{topic="notifications",type="websocket"} 45123
```

---

## Rate Limiting

All endpoints are subject to rate limiting.

**Headers:**

- `X-RateLimit-Limit`: Maximum requests per window
- `X-RateLimit-Remaining`: Remaining requests in current window
- `X-RateLimit-Reset`: Unix timestamp when the limit resets

**Example:**

```
HTTP/1.1 200 OK
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 953
X-RateLimit-Reset: 1696852800
```

**Rate Limit Exceeded:**

```
HTTP/1.1 429 Too Many Requests
Retry-After: 30
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1696852800

{
  "error": "RATE_LIMIT_EXCEEDED",
  "message": "Rate limit exceeded. Try again in 30 seconds."
}
```

---

## Error Handling

All errors follow a consistent format:

```json
{
  "error": "ERROR_CODE",
  "message": "Human-readable error message",
  "details": {
    "field": "Additional context"
  },
  "request_id": "req_abc123"
}
```

**Common Error Codes:**

| Code | HTTP Status | Description |
|------|-------------|-------------|
| INVALID_REQUEST | 400 | Malformed request |
| UNAUTHORIZED | 401 | Authentication failed |
| FORBIDDEN | 403 | Insufficient permissions |
| NOT_FOUND | 404 | Resource not found |
| PAYLOAD_TOO_LARGE | 413 | Message exceeds size limit |
| RATE_LIMIT_EXCEEDED | 429 | Rate limit exceeded |
| INTERNAL_ERROR | 500 | Server error |
| SERVICE_UNAVAILABLE | 503 | Service temporarily unavailable |

---

## Versioning

The API uses URL-based versioning:

- Current version: `v1`
- Base URL: `/api/v1`

Future versions will be available at `/api/v2`, `/api/v3`, etc.

Deprecated versions will be supported for at least 6 months after the next version is released.
