import asyncio
import json
import os

import websockets

RELAY_FQDN = os.getenv("RELAY_FQDN", "localhost:8088")


async def consume_messages():
    uri = f"ws://{RELAY_FQDN}/ws"

    async with websockets.connect(uri) as websocket:
        # Subscribe
        await websocket.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "topics": ["notifications", "alerts"],
                    "client_id": "test",
                }
            )
        )

        # Receive messages
        async for message in websocket:
            data = json.loads(message)
            print(f"Received: {data}")

            if data["type"] == "message":
                print(f"Message payload: {data['payload']}")


asyncio.run(consume_messages())
