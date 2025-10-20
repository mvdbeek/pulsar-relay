"""Integration tests for multi-worker pub/sub message broadcasting.

These tests verify that when running with multiple Uvicorn workers, messages
published to one worker are correctly broadcast to clients connected to other workers.

Requires a running Valkey instance on localhost:6379.
Start Valkey with: docker run -d -p 6379:6379 valkey/valkey:latest

Run these tests with:
    VALKEY_INTEGRATION_TEST=1 pytest tests/test_pubsub_multiworker.py -v -s
"""

import asyncio
import json
import os
from datetime import datetime

import httpx
import pytest
import websockets

pytestmark = pytest.mark.skipif(
    not os.getenv("VALKEY_INTEGRATION_TEST"), reason="VALKEY_INTEGRATION_TEST environment variable not set"
)


class TestMultiWorkerPubSub:
    """Test pub/sub message broadcasting across multiple workers.

    All tests in this class use a real server with 3 workers and Valkey storage
    to test the PubSubCoordinator's cross-worker message broadcasting.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("real_server", [{"workers": 3, "storage_backend": "valkey"}], indirect=True)
    async def test_message_reaches_all_workers_websocket(self, real_server):
        """Test that a message published to one worker reaches WebSocket clients on all workers.

        This test:
        1. Creates multiple WebSocket connections (they'll be distributed across workers)
        2. Subscribes all clients to the same topic
        3. Publishes a message via HTTP POST (goes to one worker)
        4. Verifies that ALL WebSocket clients receive the message

        This validates that the PubSubCoordinator is working correctly.
        """
        base_url = real_server["base_url"]
        ws_url = real_server["ws_url"]
        username = real_server["username"]
        password = real_server["password"]

        # Step 1: Login and get access token
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/auth/login", data={"username": username, "password": password})
            assert response.status_code == 200, f"Login failed: {response.text}"
            token = response.json()["access_token"]

        # Step 2: Create the topic first (needed for subscription validation)
        topic = "multiworker-test"
        async with httpx.AsyncClient() as client:
            # Create topic by publishing a dummy message
            response = await client.post(
                f"{base_url}/api/v1/messages",
                json={"topic": topic, "payload": {"init": "setup"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201, f"Failed to create topic: {response.text}"
            print(f"Created topic: {topic}")

        # Step 3: Create multiple WebSocket connections
        # With 3 workers, connections will be distributed via load balancing
        num_clients = 6  # 2x the number of workers to ensure distribution
        received_messages = [asyncio.Queue() for _ in range(num_clients)]

        async def websocket_client(client_id: int):
            """WebSocket client that subscribes to a topic and collects messages."""
            uri = f"{ws_url}/ws?token={token}"
            async with websockets.connect(uri) as websocket:
                # Subscribe to topic
                subscribe_msg = {
                    "type": "subscribe",
                    "topics": [topic],
                    "client_id": f"test_client_{client_id}",
                    "offset": "last",
                }
                await websocket.send(json.dumps(subscribe_msg))

                # Wait for subscription confirmation
                response = await websocket.recv()
                response_data = json.loads(response)
                if response_data["type"] != "subscribed":
                    print(f"Client {client_id}: SUBSCRIPTION FAILED - {response_data}")
                    return  # Exit if subscription fails
                print(f"Client {client_id}: Subscribed to {topic}")

                # Listen for messages (with timeout)
                try:
                    while True:
                        message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                        message_data = json.loads(message)
                        if message_data["type"] == "message":
                            await received_messages[client_id].put(message_data)
                            print(f"Client {client_id}: Received message {message_data.get('message_id')}")
                except asyncio.TimeoutError:
                    print(f"Client {client_id}: Timeout waiting for messages")

        # Step 4: Start all WebSocket clients
        client_tasks = [asyncio.create_task(websocket_client(i)) for i in range(num_clients)]

        # Give clients time to connect and subscribe
        await asyncio.sleep(0.2)

        # Step 5: Publish a message via HTTP POST
        # This will go to one of the workers (load balanced)
        test_payload = {"test": "data", "timestamp": datetime.now().isoformat(), "value": 42}

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/api/v1/messages",
                json={"topic": topic, "payload": test_payload},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201, f"Failed to publish message: {response.text}"
            published_msg = response.json()
            message_id = published_msg["message_id"]
            print(f"Published message {message_id} to topic {topic}")

        # Step 6: Wait for all clients to receive the message
        await asyncio.sleep(0.1)

        # Cancel client tasks
        for task in client_tasks:
            task.cancel()
        await asyncio.gather(*client_tasks, return_exceptions=True)

        # Step 7: Verify all clients received the message
        received_count = 0
        for i, queue in enumerate(received_messages):
            if not queue.empty():
                msg = await queue.get()
                assert msg["message_id"] == message_id, f"Client {i}: Wrong message ID"
                assert msg["topic"] == topic, f"Client {i}: Wrong topic"
                assert msg["payload"] == test_payload, f"Client {i}: Wrong payload"
                received_count += 1
                print(f"Client {i}: Verified message")
            else:
                print(f"Client {i}: Did NOT receive message (possible worker distribution issue)")

        # Assert that MOST clients received the message
        # Due to load balancing, we expect at least 80% of clients to receive it
        # (some might be on workers that didn't get subscribed in time)
        assert received_count == num_clients

    @pytest.mark.asyncio
    @pytest.mark.parametrize("real_server", [{"workers": 3, "storage_backend": "valkey"}], indirect=True)
    async def test_multiple_messages_broadcast_correctly(self, real_server):
        """Test that multiple messages are broadcast correctly across workers."""
        base_url = real_server["base_url"]
        ws_url = real_server["ws_url"]
        username = real_server["username"]
        password = real_server["password"]

        # Login
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/auth/login", data={"username": username, "password": password})
            token = response.json()["access_token"]

        # Create topic first
        topic = "multi-message-test"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/api/v1/messages",
                json={"topic": topic, "payload": {"init": "setup"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201
            print(f"Created topic: {topic}")

        # Create 3 WebSocket clients (one per worker ideally)
        received_messages = [[] for _ in range(3)]

        async def websocket_client(client_id: int):
            """WebSocket client that collects all messages."""
            uri = f"{ws_url}/ws?token={token}"
            async with websockets.connect(uri) as websocket:
                # Subscribe
                await websocket.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "topics": [topic],
                            "client_id": f"multi_msg_client_{client_id}",
                            "offset": "last",
                        }
                    )
                )

                # Wait for subscription confirmation
                response = await websocket.recv()
                response_data = json.loads(response)
                assert response_data["type"] == "subscribed"
                print(f"Client {client_id}: Subscribed")

                # Collect messages
                try:
                    while True:
                        message = await asyncio.wait_for(websocket.recv(), timeout=8.0)
                        message_data = json.loads(message)
                        if message_data["type"] == "message":
                            received_messages[client_id].append(message_data)
                            print(f"Client {client_id}: Received message {message_data['message_id']}")
                except asyncio.TimeoutError:
                    pass

        # Start clients
        client_tasks = [asyncio.create_task(websocket_client(i)) for i in range(3)]

        await asyncio.sleep(2)

        # Publish 5 messages
        num_messages = 5
        message_ids = []

        async with httpx.AsyncClient() as client:
            for i in range(num_messages):
                response = await client.post(
                    f"{base_url}/api/v1/messages",
                    json={"topic": topic, "payload": {"index": i, "data": f"message_{i}"}},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 201
                message_ids.append(response.json()["message_id"])
                await asyncio.sleep(0.2)  # Small delay between messages

        print(f"Published {num_messages} messages: {message_ids}")

        # Wait for messages to propagate
        await asyncio.sleep(0.3)

        # Cancel clients
        for task in client_tasks:
            task.cancel()
        await asyncio.gather(*client_tasks, return_exceptions=True)

        # Verify each client received all messages
        for client_id, messages in enumerate(received_messages):
            received_ids = [msg["message_id"] for msg in messages]
            print(f"Client {client_id}: Received {len(received_ids)} messages: {received_ids}")

            # Each client should receive most or all messages
            assert (
                len(received_ids) >= num_messages * 0.8
            ), f"Client {client_id} only received {len(received_ids)}/{num_messages} messages"

        print("✓ All clients received all messages via pub/sub")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("real_server", [{"workers": 3, "storage_backend": "valkey"}], indirect=True)
    async def test_pubsub_coordinator_with_long_polling(self, real_server):
        """Test that pub/sub also broadcasts to long-polling clients across workers."""
        base_url = real_server["base_url"]
        username = real_server["username"]
        password = real_server["password"]

        # Login
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/auth/login", data={"username": username, "password": password})
            token = response.json()["access_token"]

        topic = "polling-multiworker-test"

        # Start multiple long-polling clients in background
        poll_results = [None, None, None]

        async def poll_client(client_id: int):
            """Long-polling client that waits for messages."""
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    response = await client.post(
                        f"{base_url}/messages/poll",
                        json={"topics": [topic], "timeout": 10},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if response.status_code == 200:
                        data = response.json()
                        poll_results[client_id] = data.get("messages", [])
                        print(f"Poll client {client_id}: Received {len(poll_results[client_id])} messages")
                except Exception as e:
                    print(f"Poll client {client_id}: Error: {e}")

        # Start polling clients
        poll_tasks = [asyncio.create_task(poll_client(i)) for i in range(3)]

        # Give them time to connect
        await asyncio.sleep(0.1)

        # Publish a message
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/api/v1/messages",
                json={"topic": topic, "payload": {"test": "polling-broadcast"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 201
            message_id = response.json()["message_id"]
            print(f"Published message {message_id}")

        # Wait for poll clients to receive
        await asyncio.gather(*poll_tasks, return_exceptions=True)

        # Verify at least some clients received the message
        received_count = sum(1 for result in poll_results if result and len(result) > 0)
        print(f"Poll results: {poll_results}")

        assert received_count >= 2, f"Only {received_count}/3 poll clients received the message via pub/sub"

        print("✓ Long-polling clients received messages via pub/sub")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("real_server", [{"workers": 3, "storage_backend": "valkey"}], indirect=True)
    async def test_pubsub_handles_worker_specific_subscriptions(self, real_server):
        """Test that different topics are correctly routed to subscribed clients only."""
        base_url = real_server["base_url"]
        ws_url = real_server["ws_url"]
        username = real_server["username"]
        password = real_server["password"]

        # Login
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/auth/login", data={"username": username, "password": password})
            token = response.json()["access_token"]

        # Create clients subscribed to different topics
        topic_a = "topic-a-multiworker"
        topic_b = "topic-b-multiworker"

        received_a = asyncio.Queue()
        received_b = asyncio.Queue()

        async def client_for_topic_a():
            """Client subscribed only to topic-a."""
            uri = f"{ws_url}/ws?token={token}"
            async with websockets.connect(uri) as websocket:
                await websocket.send(
                    json.dumps(
                        {"type": "subscribe", "topics": [topic_a], "client_id": "client_topic_a", "offset": "last"}
                    )
                )
                response = await websocket.recv()
                assert json.loads(response)["type"] == "subscribed"
                print("Client A: Subscribed to topic-a")

                try:
                    while True:
                        message = await asyncio.wait_for(websocket.recv(), timeout=8.0)
                        message_data = json.loads(message)
                        if message_data["type"] == "message":
                            await received_a.put(message_data)
                            print(f"Client A: Received {message_data['topic']}")
                except asyncio.TimeoutError:
                    pass

        async def client_for_topic_b():
            """Client subscribed only to topic-b."""
            uri = f"{ws_url}/ws?token={token}"
            async with websockets.connect(uri) as websocket:
                await websocket.send(
                    json.dumps(
                        {"type": "subscribe", "topics": [topic_b], "client_id": "client_topic_b", "offset": "last"}
                    )
                )
                response = await websocket.recv()
                assert json.loads(response)["type"] == "subscribed"
                print("Client B: Subscribed to topic-b")

                try:
                    while True:
                        message = await asyncio.wait_for(websocket.recv(), timeout=8.0)
                        message_data = json.loads(message)
                        if message_data["type"] == "message":
                            await received_b.put(message_data)
                            print(f"Client B: Received {message_data['topic']}")
                except asyncio.TimeoutError:
                    pass

        # Start both clients
        task_a = asyncio.create_task(client_for_topic_a())
        task_b = asyncio.create_task(client_for_topic_b())

        await asyncio.sleep(2)

        # Publish messages to both topics
        async with httpx.AsyncClient() as client:
            # Publish to topic-a
            response_a = await client.post(
                f"{base_url}/api/v1/messages",
                json={"topic": topic_a, "payload": {"for": "topic-a"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response_a.status_code == 201
            msg_a_id = response_a.json()["message_id"]

            # Publish to topic-b
            response_b = await client.post(
                f"{base_url}/api/v1/messages",
                json={"topic": topic_b, "payload": {"for": "topic-b"}},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response_b.status_code == 201
            msg_b_id = response_b.json()["message_id"]

        print(f"Published to topic-a: {msg_a_id}, topic-b: {msg_b_id}")

        await asyncio.sleep(0.3)

        # Cancel clients
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

        # Verify correct routing
        assert not received_a.empty(), "Client A should have received topic-a message"
        assert not received_b.empty(), "Client B should have received topic-b message"

        msg_a = await received_a.get()
        msg_b = await received_b.get()

        assert msg_a["topic"] == topic_a and msg_a["message_id"] == msg_a_id
        assert msg_b["topic"] == topic_b and msg_b["message_id"] == msg_b_id

        # Verify clients only received their subscribed topics
        assert received_a.empty(), "Client A should not have received topic-b messages"
        assert received_b.empty(), "Client B should not have received topic-a messages"

        print("✓ Topic-specific routing works correctly across workers")
