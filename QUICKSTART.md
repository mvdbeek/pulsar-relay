# Quick Start Guide

Get the Pulsar Relay up and running in minutes with persistent message storage.

## Architecture Overview

```
┌─────────────┐
│  Producers  │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────┐
│         Pulsar Relay                │
│                                     │
│  ┌──────────┐   ┌──────────────┐   │
│  │In-Memory │──▶│    Valkey    │   │
│  │  Buffer  │   │   Streams    │   │
│  │ (Hot)    │   │ (Persistent) │   │
│  └──────────┘   └──────────────┘   │
│                                     │
│  WebSocket Server │ Long-Poll API   │
└───────┬──────────────────┬──────────┘
        │                  │
        ▼                  ▼
   ┌─────────┐      ┌─────────┐
   │WS Client│      │LP Client│
   └─────────┘      └─────────┘
```

## Option 1: Docker Compose (Recommended)

### Start the Stack

```bash
# Clone or navigate to the project
cd pulsar-relay

# Start Valkey and Relay
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

### Verify Valkey Persistence

```bash
# Check persistence configuration
docker-compose exec valkey valkey-cli CONFIG GET appendonly
# Should return: 1) "appendonly" 2) "yes"

# Check AOF status
docker-compose exec valkey valkey-cli INFO persistence
# Look for: aof_enabled:1

# View data directory
docker-compose exec valkey ls -lh /data
# Should show: appendonly.aof and dump.rdb
```

### Test the System

```bash
# Send a test message
curl -X POST http://localhost:8080/api/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "test",
    "payload": {"message": "Hello, Persistent World!"}
  }'

# Check if message was persisted
docker-compose exec valkey valkey-cli XLEN topic:test:stream
# Should return: (integer) 1

# Restart the stack to test persistence
docker-compose restart valkey

# After restart, check if message still exists
docker-compose exec valkey valkey-cli XLEN topic:test:stream
# Should still return: (integer) 1
```

## Option 2: Manual Setup

### 1. Start Valkey with Persistence

```bash
# Create data directory
mkdir -p ./data/valkey

# Start Valkey with custom config
docker run -d \
  --name pulsar-valkey \
  -p 6379:6379 \
  -v $(pwd)/valkey.conf:/etc/valkey/valkey.conf:ro \
  -v $(pwd)/data/valkey:/data \
  valkey/valkey:latest \
  valkey-server /etc/valkey/valkey.conf

# Verify it's running with persistence
docker exec pulsar-valkey valkey-cli INFO persistence | grep aof_enabled
# Should show: aof_enabled:1
```

### 2. Build and Run the Relay

```bash
# Build the relay (requires Go 1.21+)
go build -o pulsar-relay ./cmd/relay

# Run with environment variables
VALKEY_HOST=localhost \
VALKEY_PORT=6379 \
./pulsar-relay
```

## Understanding Valkey Persistence

### AOF (Append-Only File)

Every write operation is logged to `appendonly.aof`:

```bash
# View AOF file size
docker-compose exec valkey ls -lh /data/appendonly.aof

# Monitor AOF rewrites (compaction)
docker-compose exec valkey valkey-cli INFO persistence | grep aof_rewrite
```

**Durability Modes:**

| Mode | Config | Data Loss | Performance |
|------|--------|-----------|-------------|
| Always | `appendfsync always` | None | Slow (~10-30ms/write) |
| Every Second | `appendfsync everysec` | <1 second | Fast (default) |
| No | `appendfsync no` | Variable | Fastest |

Default configuration uses `everysec` for best balance.

### RDB (Snapshots)

Periodic snapshots saved to `dump.rdb`:

```bash
# View RDB file
docker-compose exec valkey ls -lh /data/dump.rdb

# Trigger manual snapshot
docker-compose exec valkey valkey-cli BGSAVE

# Check last save time
docker-compose exec valkey valkey-cli LASTSAVE
```

**Snapshot Triggers (from valkey.conf):**
- After 900 seconds if 1+ keys changed
- After 300 seconds if 10+ keys changed
- After 60 seconds if 10000+ keys changed

### Recovery Testing

Test crash recovery:

```bash
# 1. Send some messages
for i in {1..100}; do
  curl -X POST http://localhost:8080/api/v1/messages \
    -H "Content-Type: application/json" \
    -d "{\"topic\":\"test\",\"payload\":{\"id\":$i}}"
done

# 2. Check message count
docker-compose exec valkey valkey-cli XLEN topic:test:stream

# 3. Simulate crash (kill Valkey)
docker-compose kill valkey

# 4. Restart Valkey
docker-compose start valkey

# 5. Verify messages recovered
docker-compose exec valkey valkey-cli XLEN topic:test:stream
# Should show same count (or count - 1 with everysec mode)
```

## Monitoring Persistence

### Key Metrics to Watch

```bash
# Memory usage
docker-compose exec valkey valkey-cli INFO memory | grep used_memory_human

# Persistence status
docker-compose exec valkey valkey-cli INFO persistence

# Stream statistics
docker-compose exec valkey valkey-cli INFO stream

# Slow operations
docker-compose exec valkey valkey-cli SLOWLOG GET 10
```

### Prometheus Metrics

Access metrics at http://localhost:9090/metrics

Key metrics:
- `relay_messages_received_total` - Total messages received
- `relay_messages_delivered_total` - Total messages delivered
- `relay_valkey_operations_total` - Valkey operation count
- `relay_valkey_errors_total` - Valkey errors

### Grafana Dashboards

Access Grafana at http://localhost:3000 (admin/admin)

Pre-configured dashboards for:
- Message throughput
- Connection stats
- Valkey performance
- System resources

## Message Retention & Cleanup

### Automatic Stream Trimming

The relay automatically trims old messages from streams:

```bash
# Check stream length
docker-compose exec valkey valkey-cli XLEN topic:notifications:stream

# Manually trim to last 10,000 messages
docker-compose exec valkey valkey-cli XTRIM topic:notifications:stream MAXLEN ~ 10000

# Trim messages older than 24 hours (requires timestamp-based IDs)
# This is handled automatically by the relay based on configuration
```

### Configuration

In `config.yaml`:

```yaml
storage:
  persistent_tier_retention: 24h  # How long to keep in Valkey
  max_messages_per_topic: 1000000 # Max messages per topic stream
```

## Backup & Recovery

### Manual Backup

```bash
# Create backup directory
mkdir -p ./backups/$(date +%Y%m%d)

# Copy RDB snapshot
docker cp pulsar-valkey:/data/dump.rdb ./backups/$(date +%Y%m%d)/

# Copy AOF file
docker cp pulsar-valkey:/data/appendonly.aof ./backups/$(date +%Y%m%d)/
```

### Automated Backups

Add to crontab:

```bash
# Backup every 6 hours
0 */6 * * * docker exec pulsar-valkey valkey-cli BGSAVE && \
  sleep 60 && \
  docker cp pulsar-valkey:/data/dump.rdb /backups/$(date +\%Y\%m\%d-\%H\%M)/
```

### Restore from Backup

```bash
# Stop services
docker-compose down

# Replace data files
cp ./backups/20251009/dump.rdb ./data/valkey/
cp ./backups/20251009/appendonly.aof ./data/valkey/

# Start services
docker-compose up -d

# Verify restoration
docker-compose exec valkey valkey-cli DBSIZE
```

## Performance Tuning

### Valkey Configuration

Key settings in `valkey.conf`:

```conf
# Memory limit (set to ~80% of available RAM)
maxmemory 8gb

# Eviction when memory full
maxmemory-policy allkeys-lru

# I/O threads (set to CPU cores - 1)
io-threads 4

# Persistence mode
appendfsync everysec  # Best balance
```

### Application Configuration

Tune based on workload:

```yaml
# config.yaml

valkey:
  pool_size: 100  # Increase for high concurrency
```

## Troubleshooting

### Valkey won't start

```bash
# Check logs
docker-compose logs valkey

# Common issues:
# 1. Permission denied on data directory
chmod -R 777 ./data/valkey

# 2. Port already in use
lsof -i :6379
```

### Messages not persisting

```bash
# Verify AOF is enabled
docker-compose exec valkey valkey-cli CONFIG GET appendonly
# Should return: 1) "appendonly" 2) "yes"

# Check for write errors
docker-compose exec valkey valkey-cli INFO persistence | grep aof_last_write_status
# Should show: aof_last_write_status:ok

# Check disk space
docker-compose exec valkey df -h /data
```

### High memory usage

```bash
# Check memory stats
docker-compose exec valkey valkey-cli INFO memory

# Check stream sizes
docker-compose exec valkey valkey-cli --scan --pattern "topic:*:stream" | \
  xargs -I {} docker-compose exec -T valkey valkey-cli XLEN {}

# Manually trim large streams
docker-compose exec valkey valkey-cli XTRIM topic:large-topic:stream MAXLEN ~ 10000
```

### Slow performance

```bash
# Check slow log
docker-compose exec valkey valkey-cli SLOWLOG GET 10

# Monitor operations in real-time
docker-compose exec valkey valkey-cli MONITOR

# Check for blocking operations
docker-compose exec valkey valkey-cli INFO stats | grep blocked_clients
```

## Next Steps

- Read [ARCHITECTURE.md](./ARCHITECTURE.md) for detailed system design
- See [API.md](./API.md) for complete API reference
- Review [README.md](./README.md) for development setup
- Monitor metrics at http://localhost:9090 (Prometheus)
- Visualize data at http://localhost:3000 (Grafana)
