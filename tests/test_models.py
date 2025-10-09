"""Tests for Pydantic models."""

import pytest
from datetime import datetime
from pydantic import ValidationError

from app.models import (
    Message,
    MessageResponse,
    WebSocketSubscribe,
    WebSocketAck,
    BulkMessageRequest,
)


class TestMessage:
    """Tests for Message model."""

    def test_valid_message(self):
        """Test creating a valid message."""
        msg = Message(
            topic="notifications",
            payload={"user_id": 123, "message": "Hello"},
            ttl=3600,
            metadata={"priority": "high"},
        )

        assert msg.topic == "notifications"
        assert msg.payload == {"user_id": 123, "message": "Hello"}
        assert msg.ttl == 3600
        assert msg.metadata == {"priority": "high"}

    def test_message_without_optional_fields(self):
        """Test message without TTL and metadata."""
        msg = Message(topic="test", payload={"data": "value"})

        assert msg.topic == "test"
        assert msg.payload == {"data": "value"}
        assert msg.ttl is None
        assert msg.metadata is None

    def test_invalid_topic_empty(self):
        """Test that empty topic raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            Message(topic="", payload={"data": "value"})

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("topic",) for e in errors)

    def test_invalid_topic_special_chars(self):
        """Test that topic with special characters raises error."""
        with pytest.raises(ValidationError) as exc_info:
            Message(topic="invalid@topic!", payload={"data": "value"})

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("topic",) and "alphanumeric" in str(e["msg"]) for e in errors)

    def test_valid_topic_with_hyphens_underscores(self):
        """Test that topic with hyphens and underscores is valid."""
        msg = Message(topic="test-topic_123", payload={"data": "value"})
        assert msg.topic == "test-topic_123"

    def test_invalid_ttl_negative(self):
        """Test that negative TTL raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            Message(topic="test", payload={"data": "value"}, ttl=-1)

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("ttl",) for e in errors)

    def test_invalid_ttl_zero(self):
        """Test that zero TTL raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            Message(topic="test", payload={"data": "value"}, ttl=0)

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("ttl",) for e in errors)


class TestMessageResponse:
    """Tests for MessageResponse model."""

    def test_valid_response(self):
        """Test creating a valid message response."""
        timestamp = datetime.utcnow()
        resp = MessageResponse(
            message_id="msg_abc123", topic="notifications", timestamp=timestamp
        )

        assert resp.message_id == "msg_abc123"
        assert resp.topic == "notifications"
        assert resp.timestamp == timestamp


class TestWebSocketSubscribe:
    """Tests for WebSocket subscription model."""

    def test_valid_subscribe(self):
        """Test creating a valid subscribe message."""
        sub = WebSocketSubscribe(
            type="subscribe", topics=["notifications", "alerts"], client_id="client_123"
        )

        assert sub.type == "subscribe"
        assert sub.topics == ["notifications", "alerts"]
        assert sub.client_id == "client_123"
        assert sub.offset == "last"

    def test_subscribe_with_custom_offset(self):
        """Test subscribe with custom offset."""
        sub = WebSocketSubscribe(
            type="subscribe",
            topics=["test"],
            client_id="client_123",
            offset="msg_12345",
        )

        assert sub.offset == "msg_12345"

    def test_invalid_type(self):
        """Test that invalid type raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            WebSocketSubscribe(type="invalid", topics=["test"], client_id="client_123")

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("type",) for e in errors)

    def test_empty_topics(self):
        """Test that empty topics list raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            WebSocketSubscribe(type="subscribe", topics=[], client_id="client_123")

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("topics",) for e in errors)

    def test_too_many_topics(self):
        """Test that more than 50 topics raises validation error."""
        topics = [f"topic_{i}" for i in range(51)]
        with pytest.raises(ValidationError) as exc_info:
            WebSocketSubscribe(type="subscribe", topics=topics, client_id="client_123")

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("topics",) for e in errors)


class TestWebSocketAck:
    """Tests for WebSocket acknowledgment model."""

    def test_valid_ack(self):
        """Test creating a valid ack message."""
        ack = WebSocketAck(type="ack", message_id="msg_123")

        assert ack.type == "ack"
        assert ack.message_id == "msg_123"


class TestBulkMessageRequest:
    """Tests for bulk message request."""

    def test_valid_bulk_request(self):
        """Test creating a valid bulk message request."""
        req = BulkMessageRequest(
            messages=[
                Message(topic="topic1", payload={"data": 1}),
                Message(topic="topic2", payload={"data": 2}),
            ]
        )

        assert len(req.messages) == 2
        assert req.messages[0].topic == "topic1"
        assert req.messages[1].topic == "topic2"

    def test_empty_messages(self):
        """Test that empty messages list raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            BulkMessageRequest(messages=[])

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("messages",) for e in errors)

    def test_too_many_messages(self):
        """Test that more than 100 messages raises validation error."""
        messages = [Message(topic=f"topic_{i}", payload={"data": i}) for i in range(101)]
        with pytest.raises(ValidationError) as exc_info:
            BulkMessageRequest(messages=messages)

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("messages",) for e in errors)
