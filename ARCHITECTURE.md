# Pulsar Proxy - System Architecture

## Overview

A high-performance message proxy system that accepts messages from producers and delivers them to clients via WebSocket or long-polling connections. Designed for scalability, reliability, and low-latency message delivery.

## System Architecture

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                        Producers                            │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP/REST API
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                  Message Ingestion Layer                     │
│  • Authentication & Authorization                            │
│  • Rate Limiting                                             │
│  • Message Validation                                        │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              Message Storage & Queue Layer                   │
│                                                              │
│  ┌──────────────┐  ┌───────────────────────────────┐       │
│  │  Hot Tier    │  │    Persistent Tier            │       │
│  │ (In-Memory)  │  │   (Valkey with AOF/RDB)       │       │
│  │  5-10 min    │  │   Configurable Retention      │       │
│  └──────────────┘  └───────────────────────────────┘       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              Connection Manager                              │
│  • Client Registry                                           │
│  • Topic Subscriptions                                       │
│  • Connection Health Monitoring                              │
└────────────┬────────────────────────────┬───────────────────┘
             │                            │
             ▼                            ▼
┌─────────────────────┐      ┌─────────────────────┐
│  WebSocket Server   │      │  Long-Polling API   │
└──────────┬──────────┘      └──────────┬──────────┘
           │                            │
           ▼                            ▼
┌─────────────────────────────────────────────────────────────┐
│                         Clients                              │
└─────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Message Ingestion Layer

**Responsibilities:**
- Accept messages from producers via REST API
- Authenticate and authorize producers
- Validate message format and content
- Apply rate limiting and backpressure
- Enqueue messages for delivery

**API Endpoints:**

```http
POST /api/v1/messages
Content-Type: application/json
Authorization: Bearer <token>

{
  "topic": "notifications",
  "payload": { "user_id": 123, "message": "Hello" },
  "ttl": 3600,
  "metadata": {
    "priority": "high",
    "correlation_id": "abc-123"
  }
}

Response: 201 Created
{
  "message_id": "msg_abc123",
  "topic": "notifications",
  "timestamp": "2025-10-09T12:00:00Z"
}
```

```http
POST /api/v1/messages/bulk
Content-Type: application/json

{
  "messages": [
    { "topic": "topic1", "payload": {...} },
    { "topic": "topic2", "payload": {...} }
  ]
}

Response: 207 Multi-Status
{
  "results": [
    { "message_id": "msg_123", "status": "accepted" },
    { "error": "invalid_topic", "status": "rejected" }
  ]
}
```

**Rate Limiting:**
- Per-client rate limits (e.g., 1000 messages/minute)
- Token bucket algorithm
- 429 Too Many Requests with Retry-After header

### 2. Message Storage & Queue Layer

**Two-Tier Architecture:**

#### Hot Tier (In-Memory)
- **Storage**: Ring buffer or Go channels
- **Retention**: Last 5-10 minutes
- **Purpose**: Ultra-low latency delivery to active consumers
- **Implementation**: Per-topic circular buffers
- **Eviction**: TTL-based or size-based
- **Characteristics**:
  - Zero disk I/O latency
  - Fastest access for recent messages
  - Messages automatically promoted from persistent tier on miss

#### Persistent Tier (Valkey)
- **Storage**: Valkey Streams with AOF + RDB persistence
- **Retention**: Configurable (hours to days)
- **Purpose**: Durable message storage, replay, and recovery
- **Features**:
  - Consumer groups for load balancing
  - Pub/Sub for real-time notifications across instances
  - Message acknowledgment tracking
  - Persistent storage with crash recovery
  - Stream trimming based on time or count

**Valkey Persistence Configuration:**

```conf
# AOF (Append-Only File) for durability
appendonly yes
appendfsync everysec           # Fsync every second (good balance)
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb

# RDB Snapshots for fast recovery
save 900 1                     # Save after 900s if 1 key changed
save 300 10                    # Save after 300s if 10 keys changed
save 60 10000                  # Save after 60s if 10000 keys changed
stop-writes-on-bgsave-error yes
rdbcompression yes

# Memory management
maxmemory-policy allkeys-lru   # Evict based on LRU when memory limit hit
maxmemory 8gb                  # Set based on instance size
```

**Key Patterns:**
- `topic:{name}:stream` - Message stream per topic
- `topic:{name}:consumers` - Consumer group tracking
- `client:{id}:offset` - Per-client offset tracking
- `message:{id}` - Individual message metadata (if needed)

**Message Lifecycle:**
1. Message arrives → Write to Valkey Stream (durable)
2. → Push to in-memory buffer
3. → Notify connection manager via Pub/Sub
4. → Deliver to active clients
5. → Stream trimming removes old messages based on retention policy

**Valkey Streams Operations:**

```bash
# Add message to stream
XADD topic:notifications * message_id msg_123 payload {...} timestamp 1234567890

# Read messages from stream
XREAD COUNT 10 STREAMS topic:notifications 0-0

# Create consumer group
XGROUP CREATE topic:notifications consumer-group-1 $ MKSTREAM

# Read with consumer group (for acknowledgment tracking)
XREADGROUP GROUP consumer-group-1 consumer-1 COUNT 10 STREAMS topic:notifications >

# Acknowledge message
XACK topic:notifications consumer-group-1 msg_123

# Trim old messages (by count)
XTRIM topic:notifications MAXLEN ~ 100000

# Trim old messages (by time)
XTRIM topic:notifications MINID <timestamp-based-id>
```

**Persistence Guarantees:**

With `appendfsync everysec`:
- **Durability**: At most 1 second of data loss on crash
- **Performance**: Minimal impact on write throughput
- **Recovery**: Automatic replay from AOF on restart

With `appendfsync always`:
- **Durability**: Zero data loss (fsync on every write)
- **Performance**: Higher latency (~10-30ms per write)
- **Recovery**: Complete message history preserved

**Data Model:**

```go
type Message struct {
    ID        string                 `json:"message_id"`
    Topic     string                 `json:"topic"`
    Payload   map[string]interface{} `json:"payload"`
    Timestamp time.Time              `json:"timestamp"`
    TTL       int                    `json:"ttl,omitempty"`
    Metadata  map[string]string      `json:"metadata,omitempty"`
}

type Client struct {
    ClientID       string    `json:"client_id"`
    Topics         []string  `json:"topics"`
    ConnectionType string    `json:"connection_type"` // "websocket" or "longpoll"
    LastSeen       time.Time `json:"last_seen"`
    Offset         string    `json:"offset"` // Last received message_id
}

type Topic struct {
    Name            string        `json:"name"`
    RetentionPolicy time.Duration `json:"retention_policy"`
    Subscribers     []string      `json:"subscribers"`
}
```

### 3. WebSocket Server

**Connection Establishment:**

```javascript
// Client connects
const ws = new WebSocket('ws://proxy.example.com/ws?token=<auth_token>');

// Subscribe to topics
ws.send(JSON.stringify({
  type: 'subscribe',
  topics: ['notifications', 'alerts'],
  client_id: 'client_abc123',
  offset: 'last'  // or specific message_id
}));

// Server confirms
{
  "type": "subscribed",
  "topics": ["notifications", "alerts"],
  "session_id": "sess_xyz789"
}
```

**Message Delivery Protocol:**

```javascript
// Server → Client: Message delivery
{
  "type": "message",
  "message_id": "msg_123",
  "topic": "notifications",
  "payload": { ... },
  "timestamp": "2025-10-09T12:00:00Z"
}

// Client → Server: Acknowledgment
{
  "type": "ack",
  "message_id": "msg_123"
}
```

**Connection Management:**
- **Heartbeat**: Ping/pong every 30 seconds
- **Reconnection**: Exponential backoff (1s, 2s, 4s, 8s, max 30s)
- **Resume Support**: Client provides last received message_id
- **Graceful Shutdown**: Server sends close frame with reason code
- **Concurrency**: One goroutine per connection for reads, shared writer pool

**Message Types:**
- `subscribe` - Subscribe to topics
- `unsubscribe` - Unsubscribe from topics
- `message` - Message delivery from server
- `ack` - Acknowledgment from client
- `error` - Error notification
- `ping`/`pong` - Heartbeat

### 4. Long-Polling Handler

**Endpoint:**

```http
GET /api/v1/poll?topics=notifications,alerts&timeout=30&since=msg_123
Authorization: Bearer <token>

Query Parameters:
- topics: comma-separated list of topics
- timeout: max wait time in seconds (default: 30, max: 60)
- since: last received message_id (optional)
- limit: max messages to return (default: 10, max: 100)
```

**Response Patterns:**

```javascript
// Messages available (200 OK)
{
  "messages": [
    {
      "message_id": "msg_124",
      "topic": "notifications",
      "payload": {...},
      "timestamp": "2025-10-09T12:00:00Z"
    }
  ],
  "next_offset": "msg_124",
  "has_more": true
}

// No messages (304 Not Modified)
{
  "messages": [],
  "next_offset": "msg_123",
  "timeout": true
}
```

**Implementation:**

```go
func handlePoll(w http.ResponseWriter, r *http.Request) {
    ctx, cancel := context.WithTimeout(r.Context(), timeout)
    defer cancel()

    // Check for existing messages
    messages := checkMessages(topics, since)
    if len(messages) > 0 {
        respondWithMessages(w, messages)
        return
    }

    // Wait for new messages or timeout
    select {
    case msg := <-subscribeToTopics(topics):
        respondWithMessages(w, []Message{msg})
    case <-ctx.Done():
        respondWithTimeout(w)
    }
}
```

**Acknowledgment:**

```http
POST /api/v1/ack
Content-Type: application/json

{
  "message_ids": ["msg_124", "msg_125"]
}
```

## Performance Targets

- **Throughput**: 10K+ messages/second per instance
- **Latency**: <100ms for message delivery (WebSocket)
- **Connections**: 10K+ concurrent WebSocket connections per instance
- **Long-polling**: 5K+ concurrent requests per instance
- **Data Loss**: <1 second of messages on crash (with `appendfsync everysec`)

## Monitoring & Observability

### Metrics (Prometheus)

**Key Metrics:**
- `proxy_connections_total{type="websocket|longpoll"}` - Active connections
- `proxy_messages_received_total{topic}` - Inbound message rate
- `proxy_messages_delivered_total{topic,type}` - Outbound message rate
- `proxy_message_latency_seconds{quantile}` - Message delivery latency
- `proxy_queue_depth{topic}` - Pending messages per topic
- `proxy_errors_total{type,code}` - Error rates by type

**Dashboards:**
- Real-time connection count by type
- Message throughput (in/out) per topic
- Latency percentiles (p50, p95, p99)
- Error rates and types
- Queue depth and backlog

### Logging

**Structured JSON Logging:**

```json
{
  "timestamp": "2025-10-09T12:00:00Z",
  "level": "info",
  "message": "Message delivered",
  "client_id": "client_abc123",
  "message_id": "msg_456",
  "topic": "notifications",
  "latency_ms": 45,
  "connection_type": "websocket"
}
```

**Log Categories:**
- Request/response logging (with sampling)
- Error tracking with stack traces
- Client connection lifecycle events
- Rate limiting events
- Performance warnings (slow queries, high latency)

### Tracing (OpenTelemetry)

**Distributed Tracing:**
- End-to-end message flow tracking
- Span breakdown:
  1. Producer request → Ingestion
  2. Ingestion → Queue write
  3. Queue → Connection manager
  4. Connection manager → Client delivery

**Trace Context Propagation:**
- Correlation IDs in message metadata
- Trace IDs in HTTP headers
- Integration with Jaeger/Zipkin

## Failure Handling & Recovery

### Valkey Persistence & Recovery

**Crash Recovery:**
1. On restart, Valkey loads data from AOF or RDB
2. AOF provides point-in-time recovery
3. RDB provides fast bulk loading
4. Proxy reconnects to Valkey automatically

**Data Retention:**
```bash
# Automatic trimming to prevent unbounded growth
# Configure per topic based on requirements

# Time-based retention (e.g., 24 hours)
XTRIM topic:notifications MINID <24-hours-ago-timestamp>

# Count-based retention (e.g., last 1M messages)
XTRIM topic:notifications MAXLEN ~ 1000000
```

**Backup Strategy:**
```bash
# RDB snapshots can be copied for backup
# Schedule periodic backups of dump.rdb

# AOF can be replayed for point-in-time recovery
# Archive AOF files periodically
```

### Circuit Breakers

```go
// Example circuit breaker for Valkey
breaker := gobreaker.NewCircuitBreaker(gobreaker.Settings{
    Name:        "valkey",
    MaxRequests: 3,
    Timeout:     60 * time.Second,
    ReadyToTrip: func(counts gobreaker.Counts) bool {
        return counts.ConsecutiveFailures > 5
    },
})
```

### Graceful Degradation

If Valkey becomes unavailable:
1. In-memory buffer continues serving recent messages
2. New messages accepted but stored in memory only
3. Writes queued for retry when Valkey recovers
4. Alert operators of degraded state
5. Optionally reject new messages if memory threshold exceeded

## Security

### Authentication & Authorization

**API Keys:**
- Per-producer API keys with scoped permissions
- Key rotation support
- Rate limiting per key

**JWT Tokens:**
- Short-lived access tokens (5-15 minutes)
- Refresh token mechanism
- Claims-based authorization (topics, operations)

**WebSocket Authentication:**
- Token passed in connection URL or header
- Token validation on connect
- Re-authentication for long-lived connections

### Transport Security

- TLS 1.3 for all connections
- Certificate pinning for internal services
- Mutual TLS (mTLS) for service-to-service communication

### Message Security

- Optional message payload encryption (AES-256)
- Message signing for integrity verification
- PII data masking in logs and metrics

## Delivery Guarantees

### At-Least-Once Delivery (Default)

- Messages may be redelivered
- Client acknowledgment required
- Retry on failure with exponential backoff
- Duplicate detection via message_id

**Flow:**
1. Server delivers message
2. Client processes and sends ACK
3. If no ACK within timeout → redeliver
4. Client deduplicates based on message_id

### At-Most-Once Delivery (Fire-and-Forget)

- No acknowledgment required
- Message delivered once, no retries
- Lower latency, higher throughput
- Use for non-critical notifications

**Configuration:**
```json
{
  "topic": "analytics",
  "delivery_mode": "at_most_once"
}
```

## Technology Stack Recommendations

### Backend
- **Language**: Go (high concurrency, low latency)
- **Web Framework**: Fiber or Gin (HTTP/REST)
- **WebSocket**: gorilla/websocket or gobwas/ws
- **Persistent Storage**: Valkey (Redis-compatible with AOF/RDB)
- **Message Format**: Protocol Buffers or JSON

### Observability
- **Metrics**: Prometheus + Grafana
- **Logging**: Structured JSON logging
- **Tracing**: OpenTelemetry (optional)
- **Valkey Monitoring**: INFO commands, slowlog, keyspace analysis

## Implementation Phases

### Phase 1: Core Functionality (Weeks 1-3)
- Message ingestion API
- In-memory message queue
- Basic WebSocket support
- Simple long-polling implementation

### Phase 2: Persistence & Reliability (Weeks 4-6)
- Valkey Streams integration
- AOF/RDB persistence configuration
- Message acknowledgment and retry logic
- Connection recovery and resume
- Stream trimming and retention policies

### Phase 3: Performance & Optimization (Weeks 7-9)
- Performance testing and tuning
- Memory usage optimization
- Connection pooling
- Load testing with realistic workloads

### Phase 4: Production Readiness (Weeks 10-12)
- Monitoring and alerting
- Security hardening
- Backup and recovery procedures
- Documentation and runbooks

## References

- [WebSocket Protocol (RFC 6455)](https://datatracker.ietf.org/doc/html/rfc6455)
- [Long Polling Best Practices](https://javascript.info/long-polling)
- [Redis Streams](https://redis.io/docs/data-types/streams/)
- [Redis Persistence](https://redis.io/docs/management/persistence/)
- [Valkey Documentation](https://valkey.io/docs/)
