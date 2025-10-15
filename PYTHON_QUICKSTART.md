# Python Implementation Quick Start

Get started with the Python/FastAPI implementation of Pulsar Relay.

## Prerequisites

- Python 3.11+ (recommended) or 3.8+
- Valkey running locally or via Docker
- pip or uv for package management

## 1. Project Setup

```bash
# Create project directory
mkdir pulsar-relay
cd pulsar-relay

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install fastapi[standard] uvicorn[standard] valkey-glide pydantic-settings \
            prometheus-fastapi-instrumentator structlog
```

## 2. Minimal Working Example

### Create `app/main.py`

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from typing import Dict, Set, List, Any
from datetime import datetime
import uuid
import asyncio
from weakref import WeakSet

app = FastAPI(title="Pulsar Relay")

# Message model
class Message(BaseModel):
    topic: str
    payload: Dict[str, Any]
    ttl: int | None = None
    metadata: Dict[str, str] | None = None

class MessageResponse(BaseModel):
    message_id: str
    topic: str
    timestamp: datetime

# Simple in-memory connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, topics: List[str]):
        await websocket.accept()
        for topic in topics:
            if topic not in self.active_connections:
                self.active_connections[topic] = set()
            self.active_connections[topic].add(websocket)

    def disconnect(self, websocket: WebSocket, topics: List[str]):
        for topic in topics:
            if topic in self.active_connections:
                self.active_connections[topic].discard(websocket)

    async def broadcast(self, topic: str, message: dict):
        if topic not in self.active_connections:
            return

        dead_connections = []
        for connection in self.active_connections[topic]:
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.append(connection)

        # Clean up dead connections
        for conn in dead_connections:
            self.active_connections[topic].discard(conn)

manager = ConnectionManager()

# REST API Endpoints
@app.post("/api/v1/messages", response_model=MessageResponse, status_code=201)
async def create_message(message: Message):
    """Send a message to a topic"""
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    timestamp = datetime.utcnow()

    # Broadcast to WebSocket subscribers
    await manager.broadcast(message.topic, {
        "type": "message",
        "message_id": message_id,
        "topic": message.topic,
        "payload": message.payload,
        "timestamp": timestamp.isoformat(),
        "metadata": message.metadata
    })

    return MessageResponse(
        message_id=message_id,
        topic=message.topic,
        timestamp=timestamp
    )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket connection for real-time message delivery"""
    topics = []

    try:
        await websocket.accept()

        # Wait for subscription message
        data = await websocket.receive_json()

        if data.get("type") == "subscribe":
            topics = data.get("topics", [])
            await manager.connect(websocket, topics)

            # Send confirmation
            await websocket.send_json({
                "type": "subscribed",
                "topics": topics,
                "timestamp": datetime.utcnow().isoformat()
            })

            # Keep connection alive and handle pings
            while True:
                data = await websocket.receive_json()

                if data.get("type") == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": datetime.utcnow().isoformat()
                    })
                elif data.get("type") == "unsubscribe":
                    unsub_topics = data.get("topics", [])
                    for topic in unsub_topics:
                        if topic in topics:
                            topics.remove(topic)
                    manager.disconnect(websocket, unsub_topics)

    except WebSocketDisconnect:
        manager.disconnect(websocket, topics)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

### Run the Server

```bash
python app/main.py
```

Or with uvicorn directly:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

## 3. Test the Implementation

### Send a Message (Producer)

```bash
curl -X POST http://localhost:8080/api/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "notifications",
    "payload": {
      "user_id": 123,
      "message": "Hello from Python!"
    }
  }'
```

### WebSocket Client (JavaScript)

```javascript
const ws = new WebSocket('ws://localhost:8080/ws');

ws.onopen = () => {
  // Subscribe to topics
  ws.send(JSON.stringify({
    type: 'subscribe',
    topics: ['notifications', 'alerts']
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Received:', data);

  if (data.type === 'message') {
    console.log('New message:', data.payload);
  }
};

// Send periodic pings
setInterval(() => {
  ws.send(JSON.stringify({ type: 'ping' }));
}, 30000);
```

### WebSocket Client (Python)

```python
import asyncio
import websockets
import json

async def consume_messages():
    uri = "ws://localhost:8080/ws"

    async with websockets.connect(uri) as websocket:
        # Subscribe
        await websocket.send(json.dumps({
            "type": "subscribe",
            "topics": ["notifications", "alerts"]
        }))

        # Receive messages
        async for message in websocket:
            data = json.loads(message)
            print(f"Received: {data}")

            if data["type"] == "message":
                print(f"Message payload: {data['payload']}")

asyncio.run(consume_messages())
```

## 4. Add Valkey Persistence

### Update `app/main.py` with Valkey

```python
from glide import AsyncClient, NodeAddress
import json

# Initialize Valkey client
valkey_client = None

@app.on_event("startup")
async def startup_event():
    global valkey_client
    valkey_client = await AsyncClient.create_client([
        NodeAddress("localhost", 6379)
    ])

@app.on_event("shutdown")
async def shutdown_event():
    if valkey_client:
        await valkey_client.close()

@app.post("/api/v1/messages", response_model=MessageResponse, status_code=201)
async def create_message(message: Message):
    """Send a message to a topic with persistence"""
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    timestamp = datetime.utcnow()

    # Persist to Valkey Stream
    await valkey_client.xadd(
        f"topic:{message.topic}:stream",
        [
            ("message_id", message_id),
            ("payload", json.dumps(message.payload)),
            ("timestamp", timestamp.isoformat()),
            ("metadata", json.dumps(message.metadata) if message.metadata else "{}")
        ]
    )

    # Broadcast to WebSocket subscribers
    await manager.broadcast(message.topic, {
        "type": "message",
        "message_id": message_id,
        "topic": message.topic,
        "payload": message.payload,
        "timestamp": timestamp.isoformat(),
        "metadata": message.metadata
    })

    return MessageResponse(
        message_id=message_id,
        topic=message.topic,
        timestamp=timestamp
    )
```

## 5. Add Prometheus Metrics

```bash
pip install prometheus-fastapi-instrumentator
```

### Update `app/main.py`

```python
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram

# Custom metrics
messages_sent = Counter(
    'relay_messages_sent_total',
    'Total messages sent',
    ['topic']
)

message_latency = Histogram(
    'relay_message_latency_seconds',
    'Message delivery latency',
    ['topic']
)

# Instrument FastAPI
instrumentator = Instrumentator()
instrumentator.instrument(app).expose(app, endpoint="/metrics")

# Use in endpoints
@app.post("/api/v1/messages", response_model=MessageResponse, status_code=201)
async def create_message(message: Message):
    with message_latency.labels(topic=message.topic).time():
        # ... existing code ...
        messages_sent.labels(topic=message.topic).inc()
        # ... rest of code ...
```

View metrics at: http://localhost:8080/metrics

## 6. Add Configuration

### Create `app/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Server
    app_name: str = "Pulsar Relay"
    http_port: int = 8080
    workers: int = 4

    # Valkey
    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""

    # Storage
    hot_tier_retention: int = 600  # 10 minutes
    max_messages_per_topic: int = 1000000

    # Limits
    max_connections_per_instance: int = 10000
    max_message_size: int = 1048576  # 1MB

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

settings = Settings()
```

### Use in `main.py`

```python
from app.config import settings

@app.on_event("startup")
async def startup_event():
    global valkey_client
    valkey_client = await AsyncClient.create_client([
        NodeAddress(settings.valkey_host, settings.valkey_port)
    ])
```

## 7. Production Deployment

### Using Gunicorn with Uvicorn Workers

```bash
pip install gunicorn
```

```bash
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8080 \
  --log-level info
```

### Using Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["gunicorn", "app.main:app", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8080"]
```

## 8. Complete Project Structure

```
pulsar-relay/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app
│   ├── config.py        # Settings
│   ├── models.py        # Pydantic models
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── messages.py  # Message endpoints
│   │   └── websocket.py # WebSocket handler
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   └── connections.py  # ConnectionManager
│   │
│   └── storage/
│       ├── __init__.py
│       └── valkey.py    # Valkey operations
│
├── tests/
│   └── test_api.py
│
├── requirements.txt
├── .env
├── Dockerfile
└── docker-compose.yml
```

## 9. Performance Tips

### Enable uvloop

```python
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
```

### Use Multiple Workers

```bash
uvicorn app.main:app --workers 4 --host 0.0.0.0 --port 8080
```

### Connection Pooling

Valkey GLIDE automatically manages connection pooling.

## 10. Testing

```bash
pip install pytest pytest-asyncio httpx
```

### Create `tests/test_api.py`

```python
import pytest
from httpx import AsyncClient
from app.main import app

@pytest.mark.asyncio
async def test_create_message():
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/messages",
            json={
                "topic": "test",
                "payload": {"message": "Hello"}
            }
        )
        assert response.status_code == 201
        data = response.json()
        assert "message_id" in data
        assert data["topic"] == "test"

@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
```

Run tests:

```bash
pytest tests/ -v
```

## Next Steps

1. Review [PYTHON_STACK.md](./PYTHON_STACK.md) for detailed stack information
2. See [ARCHITECTURE.md](./ARCHITECTURE.md) for system design
3. Check [API.md](./API.md) for complete API reference
4. Add authentication (JWT tokens)
5. Implement long-polling endpoints
6. Add rate limiting with slowapi
7. Set up CI/CD pipeline

## Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Valkey GLIDE Python](https://github.com/valkey-io/valkey-glide/tree/main/python)
- [Pydantic Documentation](https://docs.pydantic.dev/)
- [Uvicorn Documentation](https://www.uvicorn.org/)
