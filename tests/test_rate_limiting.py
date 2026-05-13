"""Tests for the slowapi rate-limit decorators.

These tests deliberately turn the autouse :func:`reset_rate_limiter`
fixture into a no-op for the duration of each test by re-resetting at
the start, exercising the decorated endpoint enough times to trip the
limit, then asserting on the 429 response.

Closes parts of API H#7: per-IP rate limits on auth and message
endpoints prevent the previously-unthrottled brute-force / DoS paths.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulsar_relay.api import messages, topics
from pulsar_relay.api.limits import limiter
from pulsar_relay.auth.dependencies import set_topic_storage, set_user_storage
from pulsar_relay.auth.jwt import create_access_token
from pulsar_relay.auth.topic_storage import InMemoryTopicStorage
from pulsar_relay.main import app
from pulsar_relay.storage.memory import MemoryStorage


@pytest.fixture
def wired_app(auth_storage):
    """Wire up the app with in-memory storage so the rate-limited
    endpoints can actually be reached. Returns a TestClient + a valid
    bearer header."""
    storage = MemoryStorage()
    messages.set_storage(storage)
    topics.set_storage(storage)
    topic_storage = InMemoryTopicStorage()
    set_topic_storage(topic_storage)
    app.state.topic_storage = topic_storage
    set_user_storage(auth_storage)
    app.state.user_storage = auth_storage

    import asyncio

    user = asyncio.run(auth_storage.get_user_by_username("user"))
    token = create_access_token(user)
    client = TestClient(app)
    return client, {"Authorization": f"Bearer {token}"}


def test_login_endpoint_rate_limited(wired_app) -> None:
    """``/auth/login`` is capped at 5/minute. The sixth invalid attempt
    returns 429."""
    client, _ = wired_app
    limiter.reset()

    for i in range(5):
        resp = client.post("/auth/login", data={"username": "user", "password": "wrong"})
        assert resp.status_code in (401, 403), f"attempt {i}: unexpected {resp.status_code}"

    # The 6th attempt within the window must be 429, not another 401.
    resp = client.post("/auth/login", data={"username": "user", "password": "wrong"})
    assert resp.status_code == 429, resp.text


def test_messages_endpoint_rate_limit_is_generous(wired_app) -> None:
    """``/api/v1/messages`` is capped at 120/minute — well above what a
    well-behaved client sends. Six requests should sail through; the
    limiter exists for accidental floods, not normal traffic."""
    client, auth = wired_app
    limiter.reset()

    for i in range(6):
        resp = client.post(
            "/api/v1/messages",
            headers=auth,
            json={"topic": "t1", "payload": {"i": i}},
        )
        assert resp.status_code in (201, 200), f"attempt {i}: {resp.status_code} {resp.text}"


def test_rate_limit_resets_between_tests(wired_app) -> None:
    """The autouse ``reset_rate_limiter`` fixture in conftest wipes the
    Limiter's state between tests so the 5/min auth cap doesn't leak
    across unrelated test modules."""
    client, _ = wired_app
    # If the previous test left state, the first attempt here would
    # already be 429.
    resp = client.post("/auth/login", data={"username": "user", "password": "wrong"})
    assert resp.status_code != 429, "Limiter state leaked across tests"
