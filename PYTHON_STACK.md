# Python Technology Stack for Pulsar Proxy

Comprehensive technology stack recommendations for implementing the message proxy in Python with FastAPI.

## Core Framework

### FastAPI 0.115+
**Why:** Modern, fast (high-performance), web framework for building APIs with Python 3.8+
- Built-in WebSocket support via Starlette
- Native async/await support
- Automatic API documentation (OpenAPI/Swagger)
- Type hints and Pydantic v2 integration
- Excellent developer experience (you're already familiar with it!)

**Installation:**
```bash
pip install "fastapi[standard]"
```

**Performance:** Near NodeJS speeds, 40% faster than alternatives in benchmarks

---

## ASGI Server

### Uvicorn 0.32+
**Why:** Fastest ASGI server with 40% more throughput than alternatives
- uvloop integration for maximum performance
- WebSocket support
- Graceful shutdown
- Hot reload for development

**Installation:**
```bash
pip install "uvicorn[standard]"  # Includes uvloop and httptools
```

**Alternative:** Hypercorn (if you need HTTP/2 or HTTP/3 support)

**Running:**
```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --workers 4
```

---

## Event Loop Optimization

### uvloop 0.20+
**Why:** 2-4x faster than standard asyncio event loop
- Drop-in replacement for asyncio
- Written in Cython on top of libuv
- Used by default when installing uvicorn[standard]

**Installation:**
```bash
pip install uvloop
```

**Usage:**
```python
import uvloop
import asyncio

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
```

---

## Valkey/Redis Client

### Option 1: Valkey GLIDE (Recommended)
**Why:** Official async client designed for Valkey and Redis OSS
- Written in Rust for performance
- Native asyncio/anyio/trio support
- Optimized for reliability and high-availability
- Full Redis Streams support (XADD, XREAD, XGROUP, etc.)

**Installation:**
```bash
pip install valkey-glide
```

**Example:**
```python
from glide import AsyncClient, NodeAddress

async def example():
    client = await AsyncClient.create_client([
        NodeAddress("localhost", 6379)
    ])

    # Add message to stream
    await client.xadd("topic:notifications:stream", [
        ("message_id", "msg_123"),
        ("payload", '{"user_id": 123}')
    ])

    # Read from stream
    messages = await client.xread({"topic:notifications:stream": "0-0"}, count=10)
```

### Option 2: valkey-py (Alternative)
**Why:** Fork of redis-py with async support
- Familiar API if you've used redis-py
- Full compatibility with Valkey and Redis
- Async and sync modes

**Installation:**
```bash
pip install valkey
```

**Example:**
```python
from valkey.asyncio import Valkey

async def example():
    client = Valkey(host='localhost', port=6379, decode_responses=True)

    # Add to stream
    await client.xadd("topic:notifications:stream", {
        "message_id": "msg_123",
        "payload": '{"user_id": 123}'
    })
```

**Recommendation:** Use **Valkey GLIDE** for new projects for better performance and official support.

---

## Data Validation & Serialization

### Pydantic v2.10+
**Why:** Data validation using Python type annotations
- 4x faster than v1 (Rust core)
- Excellent FastAPI integration
- Automatic JSON schema generation
- Built-in validation for message models

**Installation:**
```bash
pip install pydantic
```

**Example:**
```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Dict, Any, Optional

class Message(BaseModel):
    message_id: str
    topic: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ttl: Optional[int] = None
    metadata: Optional[Dict[str, str]] = None

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "message_id": "msg_123",
                "topic": "notifications",
                "payload": {"user_id": 123, "message": "Hello"}
            }]
        }
    }
```

### Optional: msgpack for Binary Serialization

**Why:** 2-3x faster and more compact than JSON
- Efficient binary format
- Maintains type information
- Good for internal communication

**Installation:**
```bash
pip install ormsgpack  # Rust-based, fastest
```

**Example:**
```python
import ormsgpack

# Serialize
data = {"user_id": 123, "message": "Hello"}
binary = ormsgpack.packb(data)

# Deserialize
data = ormsgpack.unpackb(binary)
```

---

## Connection Management

### Built-in asyncio with Custom ConnectionManager

**Why:** Native Python async primitives are sufficient
- asyncio.Queue for message buffering
- asyncio.Lock for thread-safe operations
- WeakSet for connection tracking

**Example:**
```python
from typing import Set, Dict, List
from weakref import WeakSet
from fastapi import WebSocket
import asyncio

class ConnectionManager:
    def __init__(self):
        # Track active WebSocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        # Message buffers per topic
        self.topic_buffers: Dict[str, asyncio.Queue] = {}

    async def connect(self, websocket: WebSocket, client_id: str, topics: List[str]):
        await websocket.accept()

        for topic in topics:
            if topic not in self.active_connections:
                self.active_connections[topic] = WeakSet()
            self.active_connections[topic].add(websocket)

    def disconnect(self, websocket: WebSocket, topics: List[str]):
        for topic in topics:
            if topic in self.active_connections:
                self.active_connections[topic].discard(websocket)

    async def broadcast_to_topic(self, topic: str, message: dict):
        if topic in self.active_connections:
            dead_connections = []
            for connection in self.active_connections[topic]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_connections.append(connection)

            # Clean up dead connections
            for conn in dead_connections:
                self.active_connections[topic].discard(conn)
```

---

## Metrics & Monitoring

### prometheus-fastapi-instrumentator 7.0+
**Why:** Automatic Prometheus metrics for FastAPI
- Zero-config setup
- Request latency histograms
- Request count counters
- Custom metrics support

**Installation:**
```bash
pip install prometheus-fastapi-instrumentator
```

**Example:**
```python
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()

# Instrument the app
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
```

### prometheus-client (Alternative)
**Why:** Official Prometheus Python client for custom metrics

**Installation:**
```bash
pip install prometheus-client
```

**Example:**
```python
from prometheus_client import Counter, Histogram, Gauge

# Custom metrics
messages_received = Counter(
    'proxy_messages_received_total',
    'Total messages received',
    ['topic']
)

message_latency = Histogram(
    'proxy_message_latency_seconds',
    'Message delivery latency',
    ['topic', 'delivery_type']
)

active_connections = Gauge(
    'proxy_connections_active',
    'Active WebSocket connections',
    ['type']
)

# Usage
messages_received.labels(topic='notifications').inc()
with message_latency.labels(topic='notifications', delivery_type='websocket').time():
    await deliver_message()
```

---

## Structured Logging

### structlog 24.4+
**Why:** Structured logging for better observability
- JSON output for log aggregation
- Contextual logging
- Performance-optimized
- AsyncIO support

**Installation:**
```bash
pip install structlog
```

**Example:**
```python
import structlog

logger = structlog.get_logger()

# Structured logging with context
await logger.ainfo(
    "message_delivered",
    client_id="client_123",
    message_id="msg_456",
    topic="notifications",
    latency_ms=45
)
```

---

## Configuration Management

### Pydantic Settings
**Why:** Type-safe configuration with environment variable support
- Built into Pydantic
- .env file support
- Validation of config values

**Installation:**
```bash
pip install pydantic-settings
```

**Example:**
```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Server
    http_port: int = 8080
    ws_port: int = 8081

    # Valkey
    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_db: int = 0
    valkey_pool_size: int = 100

    # Storage
    hot_tier_retention: int = 600  # 10 minutes in seconds
    persistent_tier_retention: int = 86400  # 24 hours
    max_messages_per_topic: int = 1000000

    # Limits
    max_connections_per_instance: int = 10000
    max_message_size: int = 1048576
    rate_limit_per_client: int = 1000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

settings = Settings()
```

---

## Testing

### pytest-asyncio 0.24+
**Why:** Async test support for pytest
- Native async test functions
- Fixtures for asyncio
- Event loop management

**Installation:**
```bash
pip install pytest pytest-asyncio httpx
```

**Example:**
```python
import pytest
from httpx import AsyncClient
from main import app

@pytest.mark.asyncio
async def test_send_message():
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
```

### pytest-benchmark (Performance Testing)
**Installation:**
```bash
pip install pytest-benchmark
```

---

## Development Tools

### Black (Code Formatting)
```bash
pip install black
black .
```

### Ruff (Linting & Import Sorting)
**Why:** 10-100x faster than flake8/pylint
```bash
pip install ruff
ruff check .
ruff format .
```

### mypy (Type Checking)
```bash
pip install mypy
mypy .
```

---

## Complete Requirements File

```txt
# requirements.txt

# Core Framework
fastapi[standard]==0.115.0
uvicorn[standard]==0.32.0
uvloop==0.20.0

# Valkey Client
valkey-glide==1.2.0

# Data Validation & Serialization
pydantic==2.10.0
pydantic-settings==2.6.0
ormsgpack==1.6.0  # Optional: for msgpack serialization

# Monitoring
prometheus-fastapi-instrumentator==7.0.0
prometheus-client==0.21.0

# Logging
structlog==24.4.0

# Development
pytest==8.3.0
pytest-asyncio==0.24.0
httpx==0.27.0
black==24.8.0
ruff==0.7.0
mypy==1.11.0
```

---

## Recommended Project Structure

```
pulsar-proxy/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # Pydantic settings
│   ├── models.py            # Pydantic models
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── messages.py      # Producer endpoints
│   │   ├── websocket.py     # WebSocket handler
│   │   └── longpoll.py      # Long-polling handler
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── connections.py   # ConnectionManager
│   │   └── auth.py          # Authentication
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── valkey.py        # Valkey client & operations
│   │   └── memory.py        # In-memory buffer
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logging.py       # Structured logging setup
│       └── metrics.py       # Custom Prometheus metrics
│
├── tests/
│   ├── __init__.py
│   ├── test_api.py
│   ├── test_websocket.py
│   └── test_storage.py
│
├── requirements.txt
├── pyproject.toml           # Project config (black, ruff, mypy)
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

---

## Performance Optimization Tips

### 1. Use uvloop
Automatically enabled with `uvicorn[standard]`, or explicitly:
```python
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
```

### 2. Connection Pooling for Valkey
```python
from glide import AsyncClient, ClusterClientConfiguration

config = ClusterClientConfiguration(
    addresses=[NodeAddress("localhost", 6379)],
    request_timeout=5000,
    client_name="pulsar-proxy"
)
client = await AsyncClient.create_client(config)
```

### 3. Use Async Everywhere
```python
# Good - fully async
async def process_message(msg: Message):
    await valkey_client.xadd(f"topic:{msg.topic}:stream", msg.dict())
    await broadcast_to_websockets(msg)

# Bad - blocking call
def process_message(msg: Message):
    time.sleep(0.1)  # Blocks entire event loop!
```

### 4. Batch Operations
```python
# Batch multiple messages in a single Valkey operation
async def batch_add_messages(messages: List[Message]):
    pipeline = valkey_client.pipeline()
    for msg in messages:
        pipeline.xadd(f"topic:{msg.topic}:stream", msg.dict())
    await pipeline.execute()
```

### 5. Use Response Model for Automatic Serialization
```python
@app.post("/api/v1/messages", response_model=MessageResponse, status_code=201)
async def create_message(message: Message):
    # FastAPI automatically serializes with Pydantic
    return await storage.save_message(message)
```

---

## Benchmarking

Expected performance with this stack:

| Metric | Target | Notes |
|--------|--------|-------|
| Messages/sec (ingestion) | 15K+ | Single worker on 4-core CPU |
| WebSocket connections | 10K+ | Per worker instance |
| Message latency (WS) | <50ms | p95 latency |
| Memory per connection | ~50KB | Including buffers |

---

## Next Steps

1. Start with minimal FastAPI app + WebSocket support
2. Add Valkey integration with Streams
3. Implement ConnectionManager for WebSocket tracking
4. Add Prometheus metrics
5. Optimize with uvloop and connection pooling
6. Load test with locust or wrk2

## References

- [FastAPI WebSocket Documentation](https://fastapi.tiangolo.com/advanced/websockets/)
- [Valkey GLIDE Python](https://github.com/valkey-io/valkey-glide/tree/main/python)
- [Pydantic v2 Performance](https://docs.pydantic.dev/latest/concepts/performance/)
- [prometheus-fastapi-instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator)
- [uvloop Performance](https://github.com/MagicStack/uvloop)
