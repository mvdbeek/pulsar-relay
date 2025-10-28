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

### Docker Deployment

For production deployments, use the official Docker image which includes both Valkey and Pulsar Relay:

```bash
# Pull the latest image
docker pull ghcr.io/mvdbeek/pulsar-relay:latest

# Run with environment variables
docker run -d \
  --name pulsar-relay \
  -p 8080:8080 \
  -e PULSAR_JWT_SECRET_KEY=your-secure-secret-key \
  -e PULSAR_LOG_LEVEL=INFO \
  -v pulsar-data:/var/lib/valkey \
  ghcr.io/mvdbeek/pulsar-relay:latest
```

The Docker image:
- Runs both Valkey and Pulsar Relay using supervisor
- Exposes port 8080 for the API
- Persists Valkey data to `/var/lib/valkey`
- Includes health checks for automatic container recovery

Available environment variables:
- `PULSAR_JWT_SECRET_KEY` - Secret key for JWT token signing (required)
- `PULSAR_LOG_LEVEL` - Log level (default: INFO)
- `PULSAR_STORAGE_BACKEND` - Storage backend (default: valkey)
- `PULSAR_VALKEY_HOST` - Valkey host (default: localhost)
- `PULSAR_VALKEY_PORT` - Valkey port (default: 6379)

## Usage

### Authentication

Pulsar Relay uses JWT-based authentication. Before sending or receiving messages, you need to obtain an access token.

#### Creating the First Admin User

On first deployment, you'll need to create an admin user directly in the storage backend. For development, you can use the test fixture function:

```python
# create_admin.py
import asyncio
from app.auth.storage import InMemoryUserStorage
from app.auth.models import UserCreate

async def create_admin():
    storage = InMemoryUserStorage()
    user_data = UserCreate(
        username="admin",
        email="admin@example.com",
        password="your-secure-password",
        permissions=["admin", "read", "write"]
    )
    user = await storage.create_user(user_data)
    print(f"Created admin user: {user.username}")

asyncio.run(create_admin())
```

For production deployments with Valkey storage, contact your system administrator to create the initial admin user.

#### Logging In

Once you have user credentials, obtain a JWT token:

```bash
# Login to get access token
curl -X POST http://localhost:8080/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=your-secure-password"

# Response:
# {
#   "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
#   "token_type": "bearer",
#   "expires_in": 3600
# }
```

Tokens expire after 1 hour by default. Save the `access_token` value to use in subsequent requests.

#### Creating Additional Users (Admin Only)

Admins can create new users via the API:

```bash
curl -X POST http://localhost:8080/auth/register \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "producer1",
    "email": "producer@example.com",
    "password": "secure-password",
    "permissions": ["write"]
  }'
```

Available permissions:
- `admin`: Full access to all operations including user management
- `read`: Can subscribe to and receive messages
- `write`: Can publish messages to topics
- Multiple permissions can be granted to a single user

### Sending Messages (Producers)

```bash
# Send a single message
curl -X POST http://localhost:8080/api/v1/messages \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
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

**Note**: Requires `write` permission. Topic access is controlled by topic ownership and permissions.

### Receiving Messages (Consumers)

#### WebSocket Client

```javascript
// Use the access token from login response
const token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."; // Your JWT token
const ws = new WebSocket(`ws://localhost:8080/ws?token=${token}`);

ws.onopen = () => {
  // Subscribe to topics
  ws.send(JSON.stringify({
    type: 'subscribe',
    topics: ['notifications', 'alerts']
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === 'subscribed') {
    console.log('Subscribed to:', data.topics);
    console.log('Session ID:', data.session_id);
  }

  if (data.type === 'message') {
    console.log('Received:', data.payload);

    // Acknowledge receipt (optional - for delivery tracking)
    ws.send(JSON.stringify({
      type: 'ack',
      message_id: data.message_id
    }));
  }

  if (data.type === 'error') {
    console.error('Error:', data.message);
  }
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
  // Token may have expired - re-authenticate and reconnect
};
```

**Note**: Requires `read` permission. You can only subscribe to topics you have access to (owned topics, public topics, or topics you've been granted access to).

#### Long-Polling Client

```javascript
// Use the access token from login response
const token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."; // Your JWT token

// Track last seen message IDs for each topic
const lastSeenIds = {};

async function poll() {
  try {
    const response = await fetch(
      'http://localhost:8080/messages/poll',
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          topics: ['notifications', 'alerts'],
          since: lastSeenIds,  // Catch up on any missed messages
          timeout: 30
        })
      }
    );

    if (response.status === 401) {
      console.error('Token expired - please re-authenticate');
      return;
    }

    const data = await response.json();

    if (data.messages && data.messages.length > 0) {
      data.messages.forEach(msg => {
        console.log('Received:', msg.payload);
        // Track the last message ID for each topic
        lastSeenIds[msg.topic] = msg.message_id;
      });
    }

    // Continue polling
    poll();
  } catch (error) {
    console.error('Polling error:', error);
    // Retry after delay
    setTimeout(poll, 5000);
  }
}

poll();
```

**Note**: Requires `read` permission and appropriate topic access. The `since` parameter automatically handles message acknowledgment by tracking which messages you've already received.

### Topic Management

Topics control access to message streams. Users with `write` permission can create topics, and topic owners can manage access.

#### Creating a Topic

```bash
curl -X POST http://localhost:8080/api/v1/topics \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "topic_name": "notifications",
    "is_public": false,
    "description": "User notification messages"
  }'
```

#### Granting Topic Access

Topic owners can grant access to other users:

```bash
# Grant access by username
curl -X POST http://localhost:8080/api/v1/topics/notifications/permissions \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "consumer1"
  }'
```

#### Listing Your Topics

```bash
curl -X GET http://localhost:8080/api/v1/topics \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

#### Making a Topic Public

Public topics can be read by any authenticated user with `read` permission:

```bash
curl -X PUT http://localhost:8080/api/v1/topics/notifications \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "is_public": true
  }'
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

### Authentication API

- `POST /auth/login` - Authenticate and obtain JWT token (OAuth2 compatible)
- `POST /auth/register` - Create new user (admin only)
- `GET /auth/me` - Get current user information
- `GET /auth/users` - List all users (admin only)
- `PATCH /auth/users/{user_id}` - Update user (admin only)
- `DELETE /auth/users/{user_id}` - Delete user (admin only)

### Topic Management API

- `POST /api/v1/topics` - Create a new topic
- `GET /api/v1/topics` - List topics accessible to current user
- `GET /api/v1/topics/{topic_name}` - Get topic details
- `PUT /api/v1/topics/{topic_name}` - Update topic metadata (owner only)
- `DELETE /api/v1/topics/{topic_name}` - Delete topic (owner only)
- `POST /api/v1/topics/{topic_name}/permissions` - Grant user access to topic (owner only)
- `GET /api/v1/topics/{topic_name}/permissions` - List topic permissions (owner only)
- `DELETE /api/v1/topics/{topic_name}/permissions/{user_id}` - Revoke user access (owner only)
- `GET /api/v1/topics/stats` - Topic statistics (admin only)

### Producer API

- `POST /api/v1/messages` - Send a single message
- `POST /api/v1/messages/bulk` - Send multiple messages

### Consumer API

- `GET /ws` - WebSocket connection endpoint (query param: `token`)
- `POST /messages/poll` - Long-polling endpoint for message retrieval
- `GET /messages/poll/stats` - Poll client statistics

### Management API

- `GET /health` - Health check endpoint (no auth required)
- `GET /ready` - Readiness check endpoint (no auth required)
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

### Authentication & Authorization

- **JWT Authentication**: All API endpoints (except health/ready) require valid JWT tokens
- **Token Expiration**: Access tokens expire after 1 hour (configurable)
- **Password Hashing**: User passwords are securely hashed using industry-standard algorithms
- **Permission-Based Access Control**:
  - `admin`: Full system access including user and topic management
  - `read`: Subscribe to and receive messages from accessible topics
  - `write`: Publish messages to accessible topics
- **Topic-Level Security**:
  - Topics are owned by the user who creates them
  - Owners can grant access to specific users
  - Public topics are readable by any authenticated user
  - Private topics are only accessible to the owner and explicitly granted users

### Best Practices

- Store JWT secret key securely via environment variables
- Use strong passwords (minimum 8 characters)
- Rotate admin passwords regularly
- Grant minimum required permissions to users
- Use HTTPS in production to protect tokens in transit
- Implement rate limiting at the reverse proxy level

## Development

### Local Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linting
tox -e lint,mypy
```

### Creating a Release

For maintainers: See [RELEASE.md](./RELEASE.md) for detailed instructions on creating a new release.

Quick summary:
1. Update version in `pyproject.toml`
2. Commit and push changes
3. Create and push a version tag: `git tag v0.2.0 && git push origin v0.2.0`
4. GitHub Actions will automatically publish to PyPI and Docker registries

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
