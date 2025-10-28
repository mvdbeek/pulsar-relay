# Pulsar Relay

A message relay system for real-time message delivery to clients via WebSocket and long-polling connections.

## Features

- **Multi-Protocol Support**: WebSocket and HTTP long-polling
- **Topic-Based Routing**: Subscribe to specific message topics
- **Two-Tier Storage**: In-memory hot tier + Valkey persistent tier with AOF/RDB
- **Simple Architecture**: No external database required, just Valkey + application

## Quick Start

### Prerequisites

- Valkey (or Redis 7+) with AOF/RDB persistence enabled
- Docker (optional)

### Installation

```bash
# Clone the repository
git clone https://github.com/mvdbeek/pulsar-relay.git
cd pulsar-relay

# Set up configuration
cp config.example.yaml config.yaml
# Edit config.yaml with your settings

# Start Valkey (if not already running)
docker run -d -p 6379:6379 -v valkey-data:/data valkey/valkey --appendonly yes
export PULSAR_STORAGE_BACKEND=valkey
export PULSAR_VALKEY_HOST=valkey.example.com
export PULSAR_JWT_SECRET_KEY=your-secure-secret-key

# Start the server (port and workers controlled by uvicorn)
uvicorn app.main:app --host 0.0.0.0 --port 9000 --workers 4
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
  persistent_tier_retention: 24h  # Valkey streams retention
  max_messages_per_topic: 1000000 # Trim streams at this count
```

## API Reference

See [API.md](./API.md) for complete API documentation.

### Producer API

- `POST /api/v1/messages` - Send a single message
- `POST /api/v1/messages/bulk` - Send multiple messages

### Consumer API

- `GET /ws` - WebSocket connection endpoint
- `GET /api/v1/poll` - Long-polling endpoint

### Management API

- `GET /health` - Health check endpoint
- `GET /ready` - Readiness check endpoint
- `GET /metrics` - Prometheus metrics

## Performance

### Benchmarks

See [BENCHMARK_RESULTS.md](./BENCHMARK_RESULTS.md)

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

## Security

- JWT authentication
- Rate limiting and request validation

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
- Issues: [GitHub Issues](https://github.com/mvdbeek/pulsar-relay/issues)
- Discussions: [GitHub Discussions](https://github.com/mvdbeek/pulsar-relay/discussions)
