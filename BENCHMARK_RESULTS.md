# Pulsar Proxy Benchmark Results

**Date:** 2025-10-13
**Duration:** ~6 seconds
**Platform:** darwin (macOS)
**Python:** 3.13.6

## Executive Summary

The Pulsar Proxy demonstrates excellent performance for message ingestion and delivery scenarios:

- **Peak Throughput:** 32,955 ops/sec (bulk ingestion - 47% improvement!)
- **Single Message Ingestion:** 1,338 msgs/sec with sub-millisecond latency
- **End-to-End Delivery:** 814 msgs/sec with ~2.1ms latency
- **WebSocket Connections:** 1,117 connections/sec
- **Broadcast Performance:** 15,731 ops/sec delivering to 30 concurrent subscribers

## Benchmark Results

### 1. Message Ingestion (Single)

**Scenario:** Sequential posting of individual messages to the API

- **Operations:** 1,000 messages
- **Duration:** 0.75 seconds
- **Throughput:** 1,338 ops/sec
- **Average Latency:** 0.75ms
- **P50 Latency:** 0.73ms
- **P95 Latency:** 0.78ms
- **P99 Latency:** 1.22ms

**Analysis:** Excellent single-message performance with sub-millisecond latency across all percentiles. The 99th percentile staying under 1.5ms indicates very consistent performance.

### 2. Bulk Message Ingestion (50 messages/batch)

**Scenario:** Posting messages in batches of 50 using the bulk API

- **Operations:** 5,000 messages (100 batches)
- **Duration:** 0.15 seconds
- **Throughput:** 32,955 ops/sec
- **Average Latency:** 1.52ms per batch
- **P50 Latency:** 1.46ms
- **P95 Latency:** 1.58ms
- **P99 Latency:** 4.19ms

**Analysis:** Outstanding bulk ingestion performance, achieving **24.6x higher throughput** than single-message ingestion. The bulk API is highly efficient for high-volume scenarios with excellent consistency (P99 under 5ms).

### 3. Concurrent Ingestion (20 concurrent clients)

**Scenario:** 20 concurrent clients simultaneously posting messages

- **Operations:** 1,000 messages
- **Duration:** 2.34 seconds
- **Throughput:** 428 ops/sec
- **Average Latency:** 28.60ms
- **P50 Latency:** 27.61ms
- **P95 Latency:** 40.96ms
- **P99 Latency:** 41.78ms

**Analysis:** Higher latency under concurrent load is expected due to contention. The throughput of 428 ops/sec with 20 concurrent clients indicates good multi-client handling.

### 4. WebSocket Subscribe (50 clients)

**Scenario:** Concurrent WebSocket connection and subscription by 50 clients

- **Operations:** 50 connections
- **Duration:** 0.04 seconds
- **Throughput:** 1,117 ops/sec (75% improvement!)
- **Average Latency:** 23.43ms
- **P50 Latency:** 22.06ms
- **P95 Latency:** 32.85ms
- **P99 Latency:** 33.74ms

**Analysis:** Very fast WebSocket connection establishment with sub-35ms latencies across all percentiles. The system can handle rapid client connections extremely efficiently.

### 5. Message Delivery (End-to-End)

**Scenario:** 20 WebSocket clients subscribing to topics and receiving messages via real-time delivery

- **Operations:** 1,000 messages delivered
- **Duration:** 1.23 seconds
- **Throughput:** 814 ops/sec
- **Average Latency:** 2.13ms (from publish to receive)
- **P50 Latency:** 2.12ms
- **P95 Latency:** 3.13ms
- **P99 Latency:** 3.67ms

**Analysis:** Excellent end-to-end delivery performance with sub-2.2ms median latency. This demonstrates the proxy can deliver messages in near real-time with minimal overhead. P99 under 4ms is outstanding.

### 6. Broadcast Performance

**Scenario:** Broadcasting 50 messages to 30 concurrent WebSocket subscribers

- **Operations:** 1,500 messages delivered (100% success!)
- **Expected:** 1,500 messages (30 clients × 50 messages)
- **Duration:** 0.10 seconds
- **Throughput:** 15,731 ops/sec
- **Average Latency:** 52.68ms
- **P50 Latency:** 58.84ms
- **P95 Latency:** 69.96ms
- **P99 Latency:** 70.32ms
- **Errors:** 0

## Performance Summary

| Benchmark | Throughput | Avg Latency | Status |
|-----------|------------|-------------|--------|
| Message Ingestion (Single) | 1,338 ops/s | 0.75ms | ✅ Excellent |
| Bulk Ingestion (50/batch) | 32,955 ops/s | 1.52ms | ✅ Excellent |
| Concurrent Ingestion (20x) | 428 ops/s | 28.60ms | ✅ Good |
| WebSocket Subscribe (50) | 1,117 ops/s | 23.43ms | ✅ Excellent |
| E2E Delivery (20 clients) | 814 ops/s | 2.13ms | ✅ Excellent |
| Broadcast (30 clients) | 15,731 ops/s | 52.68ms | ✅ Excellent |

## Recommendations

### Next Steps for Production

1. **Load Testing:** Run sustained load tests (minutes/hours) to identify memory leaks
2. **Stress Testing:** Test with higher client counts (100s, 1000s) to find system limits
3. **Monitoring:** Set up Prometheus + Grafana dashboards for production monitoring
4. **Rate Limiting:** Implement rate limiting with slowapi to prevent DoS
5. **Authentication:** Add JWT authentication for production use
6. **Horizontal Scaling:** Consider Redis Pub/Sub for cross-instance messaging
7. **Valkey Integration:** Implement the Valkey storage backend for persistence

## Running the Benchmarks

To reproduce these results:

```bash
# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8080

# In another terminal, run benchmarks
python benchmarks/run_benchmarks.py
```

## Files

- Benchmark Suite: `benchmarks/run_benchmarks.py`
- Test Results: This document
- Source Code: `app/` directory
