# Pulsar Proxy

A high-performance message proxy system for real-time message delivery to clients via WebSocket and long-polling connections.

## Features

- **Multi-Protocol Support**: WebSocket and HTTP long-polling
- **High Throughput**: 10K+ messages/second per instance
- **Low Latency**: <100ms message delivery
- **Reliable Delivery**: At-least-once and at-most-once delivery modes
- **Topic-Based Routing**: Subscribe to specific message topics
- **Two-Tier Storage**: In-memory hot tier + Valkey persistent tier with AOF/RDB
- **Durable Persistence**: <1 second data loss on crash with configurable retention
- **Simple Architecture**: No external database required, just Valkey + application

## Quick Start

### Prerequisites

- Go 1.21+
- Valkey (or Redis 7+) with AOF/RDB persistence enabled
- Docker (optional)

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/pulsar-proxy.git
cd pulsar-proxy

# Install dependencies
go mod download

# Set up configuration
cp config.example.yaml config.yaml
# Edit config.yaml with your settings

# Start Valkey (if not already running)
docker run -d -p 6379:6379 -v valkey-data:/data valkey/valkey --appendonly yes

# Start the server
make run
```

### Using Docker

```bash
# Build the image
docker build -t pulsar-proxy:latest .

# Run with Docker Compose
docker-compose up -d
```

## Usage

### Sending Messages (Producers)

```bash
# Send a single message
curl -X POST http://localhost:8080/api/v1/messages \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "notifications",
    "payload": {
      "user_id": 123,
      "message": "Hello, World!"
    },
    "ttl": 3600
  }'
```

### Receiving Messages (Consumers)

#### WebSocket Client

```javascript
const ws = new WebSocket('ws://localhost:8080/ws?token=YOUR_TOKEN');

ws.onopen = () => {
  // Subscribe to topics
  ws.send(JSON.stringify({
    type: 'subscribe',
    topics: ['notifications', 'alerts'],
    client_id: 'client-123'
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === 'message') {
    console.log('Received:', data.payload);

    // Acknowledge receipt
    ws.send(JSON.stringify({
      type: 'ack',
      message_id: data.message_id
    }));
  }
};
```

#### Long-Polling Client

```javascript
async function poll() {
  const response = await fetch(
    'http://localhost:8080/api/v1/poll?topics=notifications&timeout=30',
    {
      headers: {
        'Authorization': 'Bearer YOUR_TOKEN'
      }
    }
  );

  const data = await response.json();

  if (data.messages && data.messages.length > 0) {
    data.messages.forEach(msg => {
      console.log('Received:', msg.payload);
    });

    // Acknowledge messages
    await fetch('http://localhost:8080/api/v1/ack', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer YOUR_TOKEN',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        message_ids: data.messages.map(m => m.message_id)
      })
    });
  }

  // Continue polling
  poll();
}

poll();
```

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md) for detailed architecture documentation.

### High-Level Overview

```
Producers → Ingestion API → Storage (Memory + Valkey Streams) → WebSocket/Long-Poll → Clients
```

### Key Components

- **Ingestion Layer**: REST API for message submission
- **Storage Layer**: Two-tier storage (hot in-memory + persistent Valkey)
- **Connection Manager**: Tracks active client connections
- **Delivery Layer**: WebSocket and long-polling servers

## Configuration

Configuration is managed via YAML file or environment variables:

```yaml
server:
  http_port: 8080
  ws_port: 8081
  read_timeout: 30s
  write_timeout: 30s

valkey:
  host: localhost
  port: 6379
  password: ""
  db: 0
  pool_size: 100
  # Persistence settings (configured in valkey.conf)
  # appendonly yes
  # appendfsync everysec

storage:
  hot_tier_retention: 10m        # In-memory buffer
  persistent_tier_retention: 24h  # Valkey streams retention
  max_messages_per_topic: 1000000 # Trim streams at this count

limits:
  max_connections_per_instance: 10000
  max_message_size: 1048576  # 1MB
  rate_limit_per_client: 1000  # messages per minute
```

## API Reference

See [API.md](./API.md) for complete API documentation.

### Producer API

- `POST /api/v1/messages` - Send a single message
- `POST /api/v1/messages/bulk` - Send multiple messages

### Consumer API

- `GET /ws` - WebSocket connection endpoint
- `GET /api/v1/poll` - Long-polling endpoint
- `POST /api/v1/ack` - Acknowledge message receipt

### Management API

- `GET /health` - Health check endpoint
- `GET /ready` - Readiness check endpoint
- `GET /metrics` - Prometheus metrics

## Running with Docker Compose

```bash
# Start all services (proxy + valkey)
docker-compose up -d

# View logs
docker-compose logs -f proxy

# Check Valkey persistence status
docker-compose exec valkey valkey-cli INFO persistence
```

## Monitoring

### Prometheus Metrics

Available at `http://localhost:8080/metrics`:

- `proxy_connections_total` - Active connections by type
- `proxy_messages_received_total` - Inbound message rate
- `proxy_messages_delivered_total` - Outbound message rate
- `proxy_message_latency_seconds` - Message delivery latency
- `proxy_queue_depth` - Pending messages per topic

### Valkey Monitoring

Monitor Valkey persistence and performance:

```bash
# Check persistence status
valkey-cli INFO persistence

# Monitor stream sizes
valkey-cli XLEN topic:notifications

# Check memory usage
valkey-cli INFO memory

# View slow queries
valkey-cli SLOWLOG GET 10
```

## Performance

### Benchmarks

Single instance (4 CPU cores, 8GB RAM):

- **Throughput**: 15,000 messages/second
- **Latency**: p50: 25ms, p95: 80ms, p99: 150ms
- **Connections**: 12,000 concurrent WebSocket connections
- **Long-polling**: 6,000 concurrent requests

### Valkey Tuning

Key configuration for optimal performance:

```conf
# valkey.conf
maxmemory 8gb
maxmemory-policy allkeys-lru
appendonly yes
appendfsync everysec  # Balance between durability and performance
save 900 1
save 300 10
save 60 10000
```

## Development

### Building

```bash
# Build binary
make build

# Run tests
make test

# Run with race detector
make test-race

# Lint code
make lint

# Generate mocks
make mocks
```

### Project Structure

```
.
├── cmd/
│   └── proxy/          # Main application entry point
├── internal/
│   ├── api/            # HTTP and WebSocket handlers
│   ├── auth/           # Authentication and authorization
│   ├── config/         # Configuration management
│   ├── storage/        # Storage layer (memory + Valkey streams)
│   ├── manager/        # Connection and subscription management
│   └── metrics/        # Prometheus metrics
├── pkg/
│   └── client/         # Client libraries
├── docs/               # Additional documentation
└── tests/
    ├── integration/    # Integration tests
    └── load/           # Load testing scripts
```

## Security

- TLS 1.3 for all connections
- JWT or API key authentication
- Rate limiting and request validation
- Optional message payload encryption
- PII data masking in logs

Valkey should be configured with authentication:

```conf
# valkey.conf
requirepass your-strong-password
```

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines.

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Write tests
5. Submit a pull request

## License

MIT License - see [LICENSE](./LICENSE) for details.

## Support

- Documentation: [docs/](./docs/)
- Issues: [GitHub Issues](https://github.com/yourusername/pulsar-proxy/issues)
- Discussions: [GitHub Discussions](https://github.com/yourusername/pulsar-proxy/discussions)

## Roadmap

- [ ] Support for message priority queues
- [ ] Message filtering and routing rules
- [ ] GraphQL subscription support
- [ ] gRPC streaming support
- [ ] Built-in message replay UI
- [ ] Stream compaction for efficient storage
- [ ] Multi-topic wildcard subscriptions
