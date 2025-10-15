# Pulsar Relay - Documentation Summary

## What Is This?

A high-performance message relay system for real-time message delivery via **WebSocket** and **HTTP long-polling**. Messages are stored in **Valkey Streams** with configurable persistence (AOF/RDB) for durability.

## Architecture Highlights

- **Two-tier storage**: In-memory hot tier (5-10 min) + Valkey persistent tier (configurable retention)
- **Dual delivery modes**: WebSocket for real-time push, long-polling for HTTP compatibility
- **No external database**: Valkey provides both queuing and persistence
- **Data durability**: <1 second data loss on crash with `appendfsync everysec`
- **Simple & scalable**: Stateless relay design with shared state in Valkey

## Technology Stack (Python/FastAPI)

| Component | Library | Why |
|-----------|---------|-----|
| **Web Framework** | FastAPI 0.115+ | Native async, WebSocket support, type hints |
| **ASGI Server** | Uvicorn 0.32+ | 40% faster than alternatives, uvloop integration |
| **Event Loop** | uvloop 0.20+ | 2-4x faster than standard asyncio |
| **Valkey Client** | Valkey GLIDE 1.2+ | Official async client, Rust-based performance |
| **Validation** | Pydantic v2.10+ | 4x faster than v1, excellent FastAPI integration |
| **Metrics** | prometheus-fastapi-instrumentator | Zero-config Prometheus metrics |
| **Logging** | structlog 24.4+ | Structured JSON logging |

## Documentation Guide

### Getting Started

1. **[PYTHON_QUICKSTART.md](./PYTHON_QUICKSTART.md)** - Start here!
   - Minimal working example in Python
   - Step-by-step setup guide
   - Complete code examples
   - Testing instructions

2. **[PYTHON_STACK.md](./PYTHON_STACK.md)** - Technology deep-dive
   - Detailed library comparisons
   - Configuration examples
   - Performance optimization tips
   - Complete requirements.txt

### System Design

3. **[ARCHITECTURE.md](./ARCHITECTURE.md)** - Technical architecture
   - Component diagrams
   - Data flow and message lifecycle
   - Valkey persistence configuration
   - Failure handling & recovery
   - Performance targets

4. **[API.md](./API.md)** - Complete API reference
   - Producer API (REST)
   - Consumer API (WebSocket & long-polling)
   - Message formats
   - Error handling
   - Rate limiting

### Deployment

5. **[QUICKSTART.md](./QUICKSTART.md)** - Valkey persistence testing
   - Docker Compose setup
   - Persistence verification
   - Recovery testing
   - Backup strategies

6. **[docker-compose.yml](./docker-compose.yml)** - Ready-to-run stack
   - Valkey with persistence
   - Relay application
   - Prometheus metrics
   - Grafana dashboards

7. **[valkey.conf](./valkey.conf)** - Production Valkey config
   - AOF/RDB persistence settings
   - Memory management
   - Performance tuning
   - Security options

## Quick Reference

### Start Developing (Python)

```bash
# Install dependencies
pip install fastapi[standard] uvicorn[standard] valkey-glide pydantic-settings

# Start Valkey
docker run -d -p 6379:6379 -v valkey-data:/data valkey/valkey --appendonly yes

# Run minimal example (see PYTHON_QUICKSTART.md)
uvicorn app.main:app --reload
```

### Start Full Stack (Docker)

```bash
# Start Valkey + Relay + Monitoring
docker-compose up -d

# Send test message
curl -X POST http://localhost:8080/api/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"topic":"test","payload":{"msg":"Hello"}}'

# Check metrics
curl http://localhost:8080/metrics
```

### Performance Targets

| Metric | Target | Configuration |
|--------|--------|---------------|
| Throughput | 15K+ msg/sec | Single worker, 4-core CPU |
| WebSocket connections | 10K+ | Per worker instance |
| Message latency (p95) | <50ms | With uvloop enabled |
| Data loss on crash | <1 second | `appendfsync everysec` |

## File Structure

```
pulsar-relay/
├── SUMMARY.md                    # This file
├── README.md                     # Project overview
│
├── PYTHON_QUICKSTART.md          # ⭐ Start here for Python
├── PYTHON_STACK.md               # Detailed tech stack
│
├── ARCHITECTURE.md               # System design
├── API.md                        # API reference
├── QUICKSTART.md                 # Valkey setup & testing
│
├── docker-compose.yml            # Full stack setup
├── valkey.conf                   # Valkey configuration
└── prometheus.yml                # Metrics collection
```

## Key Design Decisions

### Why Valkey Instead of PostgreSQL?

✅ **Simpler stack**: One less service to manage
✅ **Better performance**: In-memory with persistence
✅ **Native streams**: Built-in support for message ordering
✅ **Configurable durability**: Trade off between speed and data loss
✅ **Easy backup**: File-based RDB/AOF snapshots

### Why FastAPI?

✅ **You're already familiar with it**
✅ **Built-in WebSocket support** via Starlette
✅ **Excellent async performance** with uvloop
✅ **Type safety** with Pydantic v2
✅ **Automatic docs** (OpenAPI/Swagger)

### Why Valkey GLIDE?

✅ **Official client** from Valkey team
✅ **Written in Rust** for performance
✅ **Native async** support (asyncio/anyio/trio)
✅ **Full Streams support** (XADD, XREAD, XGROUP)
✅ **High-availability** features built-in

## Message Flow

```
Producer
  │
  ▼
POST /api/v1/messages
  │
  ├─► Validate (Pydantic)
  │
  ├─► Persist to Valkey Stream (XADD)
  │    └─► AOF: fsync every second
  │    └─► RDB: periodic snapshots
  │
  ├─► Push to in-memory buffer
  │
  └─► Broadcast to WebSocket subscribers
       │
       ▼
    Consumer (WebSocket/Long-Poll)
```

## Valkey Persistence Modes

| Mode | Config | Data Loss | Performance | Use Case |
|------|--------|-----------|-------------|----------|
| **Balanced** | `appendfsync everysec` | <1 second | Fast | Production (default) |
| **Durable** | `appendfsync always` | None | Slower (~10-30ms) | Critical data |
| **Fast** | `appendfsync no` | Variable | Fastest | Non-critical |

## Common Operations

### Check Message Count

```bash
docker-compose exec valkey valkey-cli XLEN topic:notifications:stream
```

### Monitor Memory

```bash
docker-compose exec valkey valkey-cli INFO memory
```

### Test Crash Recovery

```bash
# Send messages
curl -X POST http://localhost:8080/api/v1/messages -H "Content-Type: application/json" \
  -d '{"topic":"test","payload":{"id":1}}'

# Count messages
docker-compose exec valkey valkey-cli XLEN topic:test:stream

# Restart Valkey
docker-compose restart valkey

# Verify recovery
docker-compose exec valkey valkey-cli XLEN topic:test:stream
```

### Trim Old Messages

```bash
# Keep last 10,000 messages
docker-compose exec valkey valkey-cli XTRIM topic:test:stream MAXLEN ~ 10000

# Remove messages older than 24 hours (application-managed)
```

## Development Workflow

1. **Read PYTHON_QUICKSTART.md** - Get minimal example running
2. **Copy code examples** - Start with provided snippets
3. **Add Valkey persistence** - Follow Valkey integration guide
4. **Add metrics** - Instrument with Prometheus
5. **Test with clients** - Use WebSocket examples
6. **Load test** - Verify performance targets
7. **Deploy** - Use Docker Compose or K8s

## Production Checklist

- [ ] Change Valkey password in `valkey.conf`
- [ ] Configure retention policies per topic
- [ ] Set up Prometheus alerting
- [ ] Enable TLS for production
- [ ] Implement JWT authentication
- [ ] Configure rate limiting
- [ ] Set up automated backups
- [ ] Test failover scenarios
- [ ] Monitor memory usage
- [ ] Set up log aggregation

## Performance Optimization

### Python-Specific

```python
# 1. Use uvloop (automatically enabled with uvicorn[standard])
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# 2. Batch Valkey operations
pipeline = valkey_client.pipeline()
for msg in messages:
    pipeline.xadd(f"topic:{msg.topic}:stream", msg.dict())
await pipeline.execute()

# 3. Use async everywhere - no blocking calls!
async def process():
    await valkey_client.xadd(...)  # Good
    # time.sleep(0.1)  # BAD - blocks event loop!
```

### Valkey Tuning

```conf
# valkey.conf
maxmemory 8gb                  # Set to 80% of available RAM
maxmemory-policy allkeys-lru   # Evict old keys when full
io-threads 4                   # Set to CPU cores - 1
appendfsync everysec           # Balance durability/performance
```

## Monitoring & Observability

| Metric | Endpoint | Tool |
|--------|----------|------|
| Application metrics | http://localhost:8080/metrics | Prometheus |
| Health check | http://localhost:8080/health | Load balancer |
| API docs | http://localhost:8080/docs | Swagger UI |
| Grafana dashboards | http://localhost:3000 | Grafana |

## Troubleshooting

### Messages not persisting

```bash
# Check AOF status
docker-compose exec valkey valkey-cli CONFIG GET appendonly
# Should return: 1) "appendonly" 2) "yes"

# Check for errors
docker-compose logs valkey | grep -i error
```

### High memory usage

```bash
# Check stream sizes
docker-compose exec valkey valkey-cli --scan --pattern "topic:*:stream" | \
  xargs -I {} docker-compose exec -T valkey valkey-cli XLEN {}

# Trim large streams
docker-compose exec valkey valkey-cli XTRIM topic:large:stream MAXLEN ~ 10000
```

### Slow performance

```bash
# Check slow log
docker-compose exec valkey valkey-cli SLOWLOG GET 10

# Monitor in real-time
docker-compose exec valkey valkey-cli MONITOR
```

## Next Steps

1. **For Python developers**: Start with [PYTHON_QUICKSTART.md](./PYTHON_QUICKSTART.md)
2. **For architects**: Read [ARCHITECTURE.md](./ARCHITECTURE.md)
3. **For ops**: Review [QUICKSTART.md](./QUICKSTART.md) and valkey.conf
4. **For API integration**: See [API.md](./API.md)

## Questions?

- **How does persistence work?** See [ARCHITECTURE.md](./ARCHITECTURE.md#valkey-persistence-configuration)
- **What libraries should I use?** See [PYTHON_STACK.md](./PYTHON_STACK.md)
- **How do I get started?** See [PYTHON_QUICKSTART.md](./PYTHON_QUICKSTART.md)
- **What's the performance?** See [ARCHITECTURE.md](./ARCHITECTURE.md#performance-targets)

---

**TL;DR**: This is a FastAPI-based message relay with Valkey persistence. Start with [PYTHON_QUICKSTART.md](./PYTHON_QUICKSTART.md) for a minimal working example, then explore other docs as needed.
