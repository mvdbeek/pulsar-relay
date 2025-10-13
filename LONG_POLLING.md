# Long Polling API

This document describes the long polling HTTP endpoint that provides a fallback mechanism for clients that cannot establish WebSocket connections.

## Overview

Long polling is a technique where the client makes an HTTP request, and the server holds the connection open until new data is available or a timeout occurs. This provides near real-time updates without requiring WebSocket support.

## When to Use Long Polling

Use long polling instead of WebSockets when:
- Client environment doesn't support WebSockets (corporate firewalls, older browsers)
- Network intermediaries block WebSocket upgrades
- You need a simpler integration without managing persistent connections
- Testing or debugging real-time functionality

## Endpoint

### POST /messages/poll

Subscribe to one or more topics and wait for new messages.

**Request Body:**

```json
{
  "topics": ["orders", "notifications"],
  "since": {
    "orders": "msg_abc123",
    "notifications": "msg_def456"
  },
  "timeout": 30
}
```

**Parameters:**

- `topics` (required): Array of topic names to subscribe to
- `since` (optional): Dictionary mapping topics to last message IDs seen. If provided, fetches any messages newer than the specified IDs before waiting for new ones
- `timeout` (optional): Maximum seconds to wait for new messages (default: 30, max: 60)

**Response:**

```json
{
  "messages": [
    {
      "topic": "orders",
      "message_id": "msg_xyz789",
      "payload": {
        "order_id": "12345",
        "status": "completed"
      },
      "timestamp": "2025-01-15T10:30:00",
      "metadata": {
        "source": "api"
      },
      "stream_id": "1234567890123-0"
    }
  ],
  "has_more": false
}
```

**Response Fields:**

- `messages`: Array of messages received
- `has_more`: Boolean indicating if there might be more messages available immediately (for pagination)

## Usage Examples

### Basic Polling Loop

```python
import requests
import time

BASE_URL = "http://localhost:8080"

def poll_messages(topics):
    """Poll for messages with automatic reconnection."""
    last_ids = {}

    while True:
        try:
            response = requests.post(
                f"{BASE_URL}/messages/poll",
                json={
                    "topics": topics,
                    "since": last_ids if last_ids else None,
                    "timeout": 30
                },
                timeout=35  # Slightly longer than server timeout
            )

            if response.status_code == 200:
                data = response.json()

                # Process messages
                for msg in data["messages"]:
                    print(f"Received: {msg['topic']} - {msg['payload']}")
                    # Track last seen message per topic
                    last_ids[msg["topic"]] = msg["message_id"]

                # Continue immediately if there might be more
                if not data["has_more"]:
                    time.sleep(0.1)  # Brief pause before next poll
            else:
                print(f"Error: {response.status_code}")
                time.sleep(5)  # Wait before retry

        except requests.Timeout:
            # Normal timeout, continue polling
            continue
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)

# Start polling
poll_messages(["orders", "notifications"])
```

### JavaScript/TypeScript Example

```typescript
interface PollRequest {
  topics: string[];
  since?: Record<string, string>;
  timeout?: number;
}

interface PollResponse {
  messages: Array<{
    topic: string;
    message_id: string;
    payload: any;
    timestamp: string;
    metadata?: Record<string, any>;
    stream_id?: string;
  }>;
  has_more: boolean;
}

async function* pollMessages(topics: string[]): AsyncGenerator<any> {
  const lastIds: Record<string, string> = {};

  while (true) {
    try {
      const response = await fetch('http://localhost:8080/messages/poll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topics,
          since: Object.keys(lastIds).length > 0 ? lastIds : undefined,
          timeout: 30
        }),
        signal: AbortSignal.timeout(35000)
      });

      if (response.ok) {
        const data: PollResponse = await response.json();

        for (const msg of data.messages) {
          yield msg;
          lastIds[msg.topic] = msg.message_id;
        }
      }
    } catch (error) {
      if (error.name === 'TimeoutError') {
        // Normal timeout, continue
        continue;
      }
      console.error('Poll error:', error);
      await new Promise(resolve => setTimeout(resolve, 5000));
    }
  }
}

// Usage
for await (const message of pollMessages(['orders', 'notifications'])) {
  console.log('Received:', message);
}
```

### Catching Up on Missed Messages

```python
# Client was offline and wants to catch up from last known position
last_seen = {
    "orders": "msg_old123",
    "notifications": "msg_old456"
}

response = requests.post(
    "http://localhost:8080/messages/poll",
    json={
        "topics": ["orders", "notifications"],
        "since": last_seen,  # Will fetch all messages after these IDs
        "timeout": 30
    }
)

data = response.json()
# data["messages"] will contain all messages since the last seen IDs
```

## Monitoring

### GET /messages/poll/stats

Get statistics about active long polling clients.

**Response:**

```json
{
  "active_waiters": 5,
  "subscribed_topics": 3,
  "topic_subscriber_counts": {
    "orders": 3,
    "notifications": 2,
    "alerts": 1
  }
}
```

## Performance Considerations

### Client-Side

1. **Timeout Handling**: Set client timeout slightly longer than server timeout
2. **Reconnection**: Implement exponential backoff for errors
3. **Message Tracking**: Track `message_id` per topic to avoid duplicates
4. **Pagination**: Check `has_more` flag and poll immediately if true

### Server-Side

- **Resource Usage**: Each polling client holds one server connection
- **Scalability**: Suitable for hundreds of concurrent pollers
- **Cleanup**: Stale waiters are automatically cleaned up after 5 minutes

## Comparison with WebSockets

| Feature | Long Polling | WebSockets |
|---------|--------------|------------|
| Browser Support | Universal | Modern browsers only |
| Firewall-Friendly | Yes | Sometimes blocked |
| Connection Overhead | Higher (HTTP per poll) | Lower (persistent) |
| Latency | ~1-2s typical | <100ms typical |
| Scalability | Moderate (100s) | High (1000s+) |
| Implementation | Simple | More complex |
| Bidirectional | No (HTTP only) | Yes |

## Error Handling

**400 Bad Request**: Invalid request parameters
```json
{
  "detail": "At least one topic required"
}
```

**422 Validation Error**: Invalid request format
```json
{
  "detail": [
    {
      "loc": ["body", "topics"],
      "msg": "ensure this value has at least 1 items",
      "type": "value_error"
    }
  ]
}
```

**500 Internal Server Error**: Server error
```json
{
  "detail": "Internal server error"
}
```

## Best Practices

1. **Always track last message IDs** per topic to enable catch-up
2. **Handle timeouts gracefully** - they're normal in long polling
3. **Implement reconnection logic** with backoff for errors
4. **Use reasonable timeouts** (30s recommended) to balance latency and overhead
5. **Process messages idempotently** - you might receive duplicates
6. **Monitor connection health** - detect and handle network issues

## Migration from WebSockets

If you have WebSocket code, migrating to long polling is straightforward:

```python
# WebSocket code
async with websockets.connect(url) as ws:
    await ws.send(json.dumps({"type": "subscribe", "topics": ["orders"]}))
    while True:
        message = await ws.recv()
        process_message(json.loads(message))

# Long polling equivalent
while True:
    response = requests.post(poll_url, json={"topics": ["orders"], "timeout": 30})
    for message in response.json()["messages"]:
        process_message(message)
```

## Limitations

- **One-way communication**: Client can only receive messages, not send via same channel
- **Higher latency**: Typical 1-2 second delay vs sub-second for WebSockets
- **More overhead**: Each poll creates a new HTTP request
- **Connection limits**: Browser limits concurrent connections per domain

## Architecture

The long polling implementation uses:

- **PollManager**: Manages active polling clients and message distribution
- **PollWaiter**: Represents a single polling client with message queue
- **Integration**: Automatically receives messages from the same publishing pipeline as WebSockets

## Testing

Run long polling tests:

```bash
# Unit and endpoint tests
pytest tests/test_polling.py -v

# Test with live server
python examples/long_polling_client.py
```

## Support

For issues or questions:
- See main README.md for general documentation
- Check `/messages/poll/stats` endpoint for debugging
- Enable DEBUG logging to see poll manager activity
