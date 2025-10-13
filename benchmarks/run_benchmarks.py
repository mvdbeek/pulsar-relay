"""Benchmark suite for Pulsar Proxy performance testing."""

import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import websockets

# Configuration
BASE_URL = "http://localhost:8080"
WS_URL = "ws://localhost:8080/ws"


@dataclass
class BenchmarkResult:
    """Result of a benchmark run."""

    name: str
    duration: float
    operations: int
    throughput: float  # ops/sec
    latencies: list[float]
    avg_latency: float
    p50_latency: float
    p95_latency: float
    p99_latency: float
    errors: int = 0


class BenchmarkRunner:
    """Runner for performance benchmarks."""

    def __init__(self, base_url: str = BASE_URL, ws_url: str = WS_URL):
        self.base_url = base_url
        self.ws_url = ws_url
        self.results: list[BenchmarkResult] = []

    def calculate_percentile(self, latencies: list[float], percentile: float) -> float:
        """Calculate percentile from latency list."""
        if not latencies:
            return 0.0
        sorted_latencies = sorted(latencies)
        index = int(len(sorted_latencies) * percentile / 100)
        return sorted_latencies[min(index, len(sorted_latencies) - 1)]

    def create_result(
        self, name: str, duration: float, operations: int, latencies: list[float], errors: int = 0
    ) -> BenchmarkResult:
        """Create a benchmark result with calculated metrics."""
        throughput = operations / duration if duration > 0 else 0
        avg_latency = statistics.mean(latencies) if latencies else 0

        return BenchmarkResult(
            name=name,
            duration=duration,
            operations=operations,
            throughput=throughput,
            latencies=latencies,
            avg_latency=avg_latency,
            p50_latency=self.calculate_percentile(latencies, 50),
            p95_latency=self.calculate_percentile(latencies, 95),
            p99_latency=self.calculate_percentile(latencies, 99),
            errors=errors,
        )

    async def benchmark_message_ingestion(self, num_messages: int = 1000) -> BenchmarkResult:
        """Benchmark single message ingestion throughput."""
        print(f"\n📊 Running: Message Ingestion ({num_messages} messages)")

        latencies = []
        errors = 0

        async with httpx.AsyncClient(base_url=self.base_url) as client:
            start_time = time.time()

            for i in range(num_messages):
                msg_start = time.time()
                try:
                    response = await client.post(
                        "/api/v1/messages",
                        json={
                            "topic": f"benchmark-topic-{i % 10}",
                            "payload": {"index": i, "data": f"message-{i}"},
                        },
                    )
                    response.raise_for_status()
                    latencies.append(time.time() - msg_start)
                except Exception as e:
                    errors += 1
                    print(f"Error: {e}")

            duration = time.time() - start_time

        result = self.create_result(
            "Message Ingestion (Single)",
            duration,
            num_messages - errors,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_bulk_ingestion(self, num_batches: int = 100, batch_size: int = 50) -> BenchmarkResult:
        """Benchmark bulk message ingestion throughput."""
        total_messages = num_batches * batch_size
        print(f"\n📊 Running: Bulk Message Ingestion ({total_messages} messages in {num_batches} batches)")

        latencies = []
        errors = 0

        async with httpx.AsyncClient(base_url=self.base_url) as client:
            start_time = time.time()

            for batch_idx in range(num_batches):
                batch_start = time.time()

                messages = [
                    {
                        "topic": f"benchmark-bulk-{i % 5}",
                        "payload": {"batch": batch_idx, "index": i},
                    }
                    for i in range(batch_size)
                ]

                try:
                    response = await client.post(
                        "/api/v1/messages/bulk",
                        json={"messages": messages},
                    )
                    response.raise_for_status()
                    latencies.append(time.time() - batch_start)
                except Exception as e:
                    errors += 1
                    print(f"Error: {e}")

            duration = time.time() - start_time

        result = self.create_result(
            f"Bulk Message Ingestion ({batch_size}/batch)",
            duration,
            total_messages - (errors * batch_size),
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_concurrent_ingestion(self, num_messages: int = 1000, concurrency: int = 10) -> BenchmarkResult:
        """Benchmark concurrent message ingestion."""
        print(f"\n📊 Running: Concurrent Ingestion ({num_messages} messages, {concurrency} concurrent)")

        latencies = []
        errors = 0

        async def send_message(client: httpx.AsyncClient, index: int):
            msg_start = time.time()
            try:
                response = await client.post(
                    "/api/v1/messages",
                    json={
                        "topic": f"concurrent-topic-{index % 20}",
                        "payload": {"index": index, "timestamp": time.time()},
                    },
                )
                response.raise_for_status()
                return time.time() - msg_start, None
            except Exception as e:
                return None, str(e)

        async with httpx.AsyncClient(base_url=self.base_url) as client:
            start_time = time.time()

            # Send messages in batches with concurrency limit
            for i in range(0, num_messages, concurrency):
                batch = range(i, min(i + concurrency, num_messages))
                results = await asyncio.gather(*[send_message(client, idx) for idx in batch])

                for latency, error in results:
                    if error:
                        errors += 1
                    else:
                        latencies.append(latency)

            duration = time.time() - start_time

        result = self.create_result(
            f"Concurrent Ingestion (concurrency={concurrency})",
            duration,
            num_messages - errors,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_websocket_subscribe(self, num_clients: int = 50) -> BenchmarkResult:
        """Benchmark WebSocket connection and subscription latency."""
        print(f"\n📊 Running: WebSocket Subscribe ({num_clients} clients)")

        latencies = []
        errors = 0

        async def connect_and_subscribe(client_id: int):
            ws_start = time.time()
            try:
                async with websockets.connect(self.ws_url) as websocket:
                    # Subscribe
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "topics": [f"ws-topic-{client_id % 5}"],
                                "client_id": f"bench-client-{client_id}",
                            }
                        )
                    )

                    # Wait for confirmation
                    response = await websocket.recv()
                    data = json.loads(response)

                    if data.get("type") == "subscribed":
                        return time.time() - ws_start, None
                    else:
                        return None, f"Unexpected response: {data}"
            except Exception as e:
                return None, str(e)

        start_time = time.time()

        results = await asyncio.gather(*[connect_and_subscribe(i) for i in range(num_clients)])

        for latency, error in results:
            if error:
                errors += 1
            else:
                latencies.append(latency)

        duration = time.time() - start_time

        result = self.create_result(
            f"WebSocket Subscribe ({num_clients} clients)",
            duration,
            num_clients - errors,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_message_delivery(self, num_clients: int = 20, messages_per_topic: int = 100) -> BenchmarkResult:
        """Benchmark end-to-end message delivery latency via WebSocket."""
        print(f"\n📊 Running: Message Delivery ({num_clients} clients, {messages_per_topic} msgs/topic)")

        latencies = []
        errors = 0
        received_count = 0

        async def websocket_consumer(client_id: int, topic: str, expected_messages: int):
            """Consumer that subscribes and receives messages."""
            nonlocal received_count, errors

            try:
                async with websockets.connect(self.ws_url) as websocket:
                    # Subscribe
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "topics": [topic],
                                "client_id": f"consumer-{client_id}",
                            }
                        )
                    )

                    # Wait for subscription confirmation
                    await websocket.recv()

                    # Receive messages
                    for _ in range(expected_messages):
                        try:
                            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                            data = json.loads(response)

                            if data.get("type") == "message":
                                # Calculate latency from message timestamp
                                msg_timestamp = data.get("payload", {}).get("sent_at", 0)
                                if msg_timestamp:
                                    latency = time.time() - msg_timestamp
                                    latencies.append(latency)
                                received_count += 1
                        except asyncio.TimeoutError:
                            errors += 1
                            break
            except Exception as e:
                errors += 1
                print(f"Consumer error: {e}")

        async def message_producer(topic: str, num_messages: int):
            """Producer that sends messages to a topic."""
            async with httpx.AsyncClient(base_url=self.base_url) as client:
                # Small delay to let consumers connect
                await asyncio.sleep(0.5)

                for i in range(num_messages):
                    try:
                        await client.post(
                            "/api/v1/messages",
                            json={
                                "topic": topic,
                                "payload": {
                                    "index": i,
                                    "sent_at": time.time(),
                                },
                            },
                        )
                        # Small delay between messages
                        await asyncio.sleep(0.01)
                    except Exception as e:
                        print(f"Producer error: {e}")

        # Create topics
        topics = [f"delivery-topic-{i}" for i in range(min(5, num_clients))]

        start_time = time.time()

        # Start consumers
        consumer_tasks = [
            websocket_consumer(i, topics[i % len(topics)], messages_per_topic) for i in range(num_clients)
        ]

        # Start producers
        producer_tasks = [message_producer(topic, messages_per_topic) for topic in topics]

        # Wait for all tasks
        await asyncio.gather(*consumer_tasks, *producer_tasks)

        duration = time.time() - start_time

        result = self.create_result(
            f"Message Delivery (E2E, {num_clients} clients)",
            duration,
            received_count,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_broadcast_performance(self, num_clients: int = 50, num_messages: int = 100) -> BenchmarkResult:
        """Benchmark broadcasting performance with multiple subscribers."""
        print(f"\n📊 Running: Broadcast Performance ({num_clients} clients, {num_messages} messages)")

        latencies = []
        errors = 0
        total_received = 0

        topic = "broadcast-benchmark"

        async def subscriber(client_id: int):
            """Subscriber that counts received messages."""
            nonlocal total_received, errors

            received = 0
            try:
                async with websockets.connect(self.ws_url) as websocket:
                    # Subscribe
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "topics": [topic],
                                "client_id": f"broadcast-client-{client_id}",
                            }
                        )
                    )

                    # Wait for confirmation
                    await websocket.recv()

                    # Receive messages
                    for _ in range(num_messages):
                        try:
                            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                            data = json.loads(response)

                            if data.get("type") == "message":
                                received += 1

                                # Track latency from send time
                                sent_at = data.get("payload", {}).get("sent_at", 0)
                                if sent_at:
                                    latencies.append(time.time() - sent_at)
                        except asyncio.TimeoutError:
                            errors += 1
                            break

                total_received += received
            except Exception as e:
                errors += 1
                print(f"Subscriber error: {e}")

        # Start all subscribers as background tasks
        subscriber_tasks = [asyncio.create_task(subscriber(i)) for i in range(num_clients)]

        # Give subscribers time to connect and subscribe
        await asyncio.sleep(1.5)

        start_time = time.time()

        # Send messages while subscribers are listening
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            send_tasks = []
            for i in range(num_messages):
                send_tasks.append(
                    client.post(
                        "/api/v1/messages",
                        json={
                            "topic": topic,
                            "payload": {"index": i, "sent_at": time.time()},
                        },
                    )
                )
            await asyncio.gather(*send_tasks)

        # Wait for all subscribers to finish receiving messages
        await asyncio.gather(*subscriber_tasks, return_exceptions=True)

        duration = time.time() - start_time

        # Expected total: num_clients * num_messages
        expected_total = num_clients * num_messages

        result = self.create_result(
            f"Broadcast ({num_clients} clients, {num_messages} msgs)",
            duration,
            total_received,
            latencies,
            errors,
        )
        self.results.append(result)

        print(f"   📬 Total messages received: {total_received}/{expected_total}")

        return result

    async def benchmark_long_polling_basic(self, num_requests: int = 100) -> BenchmarkResult:
        """Benchmark basic long polling request/response latency."""
        print(f"\n📊 Running: Long Polling Basic ({num_requests} requests)")

        latencies = []
        errors = 0

        async with httpx.AsyncClient(base_url=self.base_url, timeout=35.0) as client:
            start_time = time.time()

            for i in range(num_requests):
                poll_start = time.time()
                try:
                    response = await client.post(
                        "/messages/poll",
                        json={
                            "topics": [f"poll-topic-{i % 5}"],
                            "timeout": 1,  # Short timeout for fast benchmark
                        },
                    )
                    response.raise_for_status()
                    latencies.append(time.time() - poll_start)
                except Exception as e:
                    errors += 1
                    print(f"Error: {e}")

            duration = time.time() - start_time

        result = self.create_result(
            "Long Polling Basic (1s timeout)",
            duration,
            num_requests - errors,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_long_polling_delivery(
        self, num_clients: int = 10, messages_per_topic: int = 20
    ) -> BenchmarkResult:
        """Benchmark end-to-end message delivery via long polling."""
        print(f"\n📊 Running: Long Polling Delivery ({num_clients} clients, {messages_per_topic} msgs/topic)")

        latencies = []
        errors = 0
        received_count = 0

        async def polling_consumer(client_id: int, topic: str, expected_messages: int):
            """Consumer that polls for messages."""
            nonlocal received_count, errors

            last_id = {}
            messages_received = 0

            try:
                async with httpx.AsyncClient(base_url=self.base_url, timeout=35.0) as client:
                    while messages_received < expected_messages:
                        try:
                            response = await client.post(
                                "/messages/poll",
                                json={
                                    "topics": [topic],
                                    "since": last_id if last_id else None,
                                    "timeout": 5,
                                },
                            )
                            response.raise_for_status()
                            data = response.json()

                            for msg in data.get("messages", []):
                                if msg["topic"] == topic:
                                    # Calculate latency
                                    sent_at = msg.get("payload", {}).get("sent_at", 0)
                                    if sent_at:
                                        latencies.append(time.time() - sent_at)

                                    last_id[topic] = msg["message_id"]
                                    messages_received += 1
                                    received_count += 1

                        except asyncio.TimeoutError:
                            errors += 1
                            break
                        except Exception as e:
                            errors += 1
                            print(f"Consumer error: {e}")
                            break

            except Exception as e:
                errors += 1
                print(f"Consumer setup error: {e}")

        async def message_producer(topic: str, num_messages: int):
            """Producer that sends messages."""
            async with httpx.AsyncClient(base_url=self.base_url) as client:
                # Small delay to let consumers start polling
                await asyncio.sleep(0.5)

                for i in range(num_messages):
                    try:
                        await client.post(
                            "/api/v1/messages",
                            json={
                                "topic": topic,
                                "payload": {
                                    "index": i,
                                    "sent_at": time.time(),
                                },
                            },
                        )
                        # Small delay between messages
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        print(f"Producer error: {e}")

        # Create topics
        topics = [f"poll-delivery-{i}" for i in range(min(5, num_clients))]

        start_time = time.time()

        # Start consumers
        consumer_tasks = [polling_consumer(i, topics[i % len(topics)], messages_per_topic) for i in range(num_clients)]

        # Start producers
        producer_tasks = [message_producer(topic, messages_per_topic) for topic in topics]

        # Wait for all tasks
        await asyncio.gather(*consumer_tasks, *producer_tasks)

        duration = time.time() - start_time

        result = self.create_result(
            f"Long Polling Delivery (E2E, {num_clients} clients)",
            duration,
            received_count,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_long_polling_concurrent(self, num_clients: int = 20, timeout: int = 5) -> BenchmarkResult:
        """Benchmark concurrent long polling clients."""
        print(f"\n📊 Running: Long Polling Concurrent ({num_clients} concurrent clients)")

        latencies = []
        errors = 0

        async def concurrent_poller(client_id: int):
            """Single polling client."""
            poll_start = time.time()
            try:
                async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout + 5.0) as client:
                    response = await client.post(
                        "/messages/poll",
                        json={
                            "topics": [f"concurrent-poll-{client_id % 10}"],
                            "timeout": timeout,
                        },
                    )
                    response.raise_for_status()
                    return time.time() - poll_start, None
            except Exception as e:
                return None, str(e)

        start_time = time.time()

        # All clients poll concurrently
        results = await asyncio.gather(*[concurrent_poller(i) for i in range(num_clients)])

        for latency, error in results:
            if error:
                errors += 1
            else:
                latencies.append(latency)

        duration = time.time() - start_time

        result = self.create_result(
            f"Long Polling Concurrent ({num_clients} clients)",
            duration,
            num_clients - errors,
            latencies,
            errors,
        )
        self.results.append(result)
        return result

    async def benchmark_polling_vs_websocket(self, num_messages: int = 50) -> dict[str, BenchmarkResult]:
        """Compare long polling vs WebSocket message delivery."""
        print(f"\n📊 Running: Long Polling vs WebSocket Comparison ({num_messages} messages each)")

        topic_ws = "comparison-websocket"
        topic_poll = "comparison-polling"

        # WebSocket benchmark
        ws_latencies = []
        ws_errors = 0
        ws_received = 0

        async def websocket_receiver():
            nonlocal ws_received, ws_errors
            try:
                async with websockets.connect(self.ws_url) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "topics": [topic_ws],
                                "client_id": "comparison-ws",
                            }
                        )
                    )
                    await websocket.recv()  # Wait for confirmation

                    for _ in range(num_messages):
                        try:
                            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                            data = json.loads(response)
                            if data.get("type") == "message":
                                sent_at = data.get("payload", {}).get("sent_at", 0)
                                if sent_at:
                                    ws_latencies.append(time.time() - sent_at)
                                ws_received += 1
                        except asyncio.TimeoutError:
                            ws_errors += 1
                            break
            except Exception as e:
                ws_errors += 1
                print(f"WebSocket error: {e}")

        # Long polling benchmark
        poll_latencies = []
        poll_errors = 0
        poll_received = 0

        async def polling_receiver():
            nonlocal poll_received, poll_errors
            last_id = {}
            try:
                async with httpx.AsyncClient(base_url=self.base_url, timeout=35.0) as client:
                    while poll_received < num_messages:
                        try:
                            response = await client.post(
                                "/messages/poll",
                                json={
                                    "topics": [topic_poll],
                                    "since": last_id if last_id else None,
                                    "timeout": 5,
                                },
                            )
                            response.raise_for_status()
                            data = response.json()

                            for msg in data.get("messages", []):
                                sent_at = msg.get("payload", {}).get("sent_at", 0)
                                if sent_at:
                                    poll_latencies.append(time.time() - sent_at)
                                last_id[msg["topic"]] = msg["message_id"]
                                poll_received += 1
                        except asyncio.TimeoutError:
                            poll_errors += 1
                            break
            except Exception as e:
                poll_errors += 1
                print(f"Polling error: {e}")

        # Producers
        async def ws_producer():
            await asyncio.sleep(0.5)
            async with httpx.AsyncClient(base_url=self.base_url) as client:
                for i in range(num_messages):
                    await client.post(
                        "/api/v1/messages",
                        json={
                            "topic": topic_ws,
                            "payload": {"index": i, "sent_at": time.time()},
                        },
                    )
                    await asyncio.sleep(0.02)

        async def poll_producer():
            await asyncio.sleep(0.5)
            async with httpx.AsyncClient(base_url=self.base_url) as client:
                for i in range(num_messages):
                    await client.post(
                        "/api/v1/messages",
                        json={
                            "topic": topic_poll,
                            "payload": {"index": i, "sent_at": time.time()},
                        },
                    )
                    await asyncio.sleep(0.02)

        # Run both benchmarks
        ws_start = time.time()
        await asyncio.gather(websocket_receiver(), ws_producer())
        ws_duration = time.time() - ws_start

        poll_start = time.time()
        await asyncio.gather(polling_receiver(), poll_producer())
        poll_duration = time.time() - poll_start

        # Create results
        ws_result = self.create_result(
            "WebSocket Delivery (comparison)",
            ws_duration,
            ws_received,
            ws_latencies,
            ws_errors,
        )

        poll_result = self.create_result(
            "Long Polling Delivery (comparison)",
            poll_duration,
            poll_received,
            poll_latencies,
            poll_errors,
        )

        self.results.extend([ws_result, poll_result])

        print(f"\n   📊 WebSocket: {len(ws_latencies)} msgs, avg {ws_result.avg_latency*1000:.2f}ms")
        print(f"   📊 Polling:   {len(poll_latencies)} msgs, avg {poll_result.avg_latency*1000:.2f}ms")

        return {"websocket": ws_result, "polling": poll_result}

    def print_result(self, result: BenchmarkResult):
        """Print a single benchmark result."""
        print(f"\n{'=' * 70}")
        print(f"📊 {result.name}")
        print(f"{'=' * 70}")
        print(f"Duration:       {result.duration:.2f}s")
        print(f"Operations:     {result.operations:,}")
        print(f"Throughput:     {result.throughput:,.0f} ops/sec")
        print(f"Avg Latency:    {result.avg_latency * 1000:.2f}ms")
        print(f"P50 Latency:    {result.p50_latency * 1000:.2f}ms")
        print(f"P95 Latency:    {result.p95_latency * 1000:.2f}ms")
        print(f"P99 Latency:    {result.p99_latency * 1000:.2f}ms")
        if result.errors > 0:
            print(f"⚠️  Errors:        {result.errors}")

    def print_summary(self):
        """Print summary of all benchmark results."""
        print(f"\n{'=' * 70}")
        print("📈 BENCHMARK SUMMARY")
        print(f"{'=' * 70}\n")

        # Table header
        print(f"{'Benchmark':<45} {'Throughput':>12} {'Avg Latency':>12}")
        print(f"{'-' * 45} {'-' * 12} {'-' * 12}")

        for result in self.results:
            throughput_str = f"{result.throughput:,.0f} ops/s"
            latency_str = f"{result.avg_latency * 1000:.2f}ms"
            print(f"{result.name:<45} {throughput_str:>12} {latency_str:>12}")

        print()

    async def run_all_benchmarks(self):
        """Run all benchmarks in sequence."""
        print("\n" + "=" * 70)
        print("🚀 PULSAR PROXY BENCHMARK SUITE")
        print("=" * 70)
        print(f"Base URL: {self.base_url}")
        print(f"WebSocket URL: {self.ws_url}")
        print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Check if server is running
        try:
            async with httpx.AsyncClient(base_url=self.base_url) as client:
                response = await client.get("/health")
                response.raise_for_status()
                print("✅ Server is healthy and ready")
        except Exception as e:
            print(f"❌ Server not available: {e}")
            print("Please start the server with: uvicorn app.main:app --reload")
            return

        # Run benchmarks
        try:
            # HTTP ingestion benchmarks
            await self.benchmark_message_ingestion(num_messages=1000)
            self.print_result(self.results[-1])

            await self.benchmark_bulk_ingestion(num_batches=100, batch_size=50)
            self.print_result(self.results[-1])

            await self.benchmark_concurrent_ingestion(num_messages=1000, concurrency=20)
            self.print_result(self.results[-1])

            # WebSocket benchmarks
            await self.benchmark_websocket_subscribe(num_clients=50)
            self.print_result(self.results[-1])

            await self.benchmark_message_delivery(num_clients=20, messages_per_topic=50)
            self.print_result(self.results[-1])

            await self.benchmark_broadcast_performance(num_clients=30, num_messages=50)
            self.print_result(self.results[-1])

            # Long polling benchmarks
            await self.benchmark_long_polling_basic(num_requests=100)
            self.print_result(self.results[-1])

            await self.benchmark_long_polling_concurrent(num_clients=20, timeout=2)
            self.print_result(self.results[-1])

            await self.benchmark_long_polling_delivery(num_clients=10, messages_per_topic=20)
            self.print_result(self.results[-1])

            # Comparison benchmark
            await self.benchmark_polling_vs_websocket(num_messages=50)
            self.print_result(self.results[-2])  # WebSocket result
            self.print_result(self.results[-1])  # Polling result

        except KeyboardInterrupt:
            print("\n⚠️  Benchmark interrupted by user")
        except Exception as e:
            print(f"\n❌ Benchmark error: {e}")
            import traceback

            traceback.print_exc()

        # Print summary
        self.print_summary()

        print(f"Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)


async def main():
    """Main entry point for benchmarks."""
    runner = BenchmarkRunner()
    await runner.run_all_benchmarks()


if __name__ == "__main__":
    asyncio.run(main())
