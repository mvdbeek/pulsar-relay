"""Contract tests for :meth:`HttpRelayClient.create_or_verify_topic`
and :meth:`HttpRelayClient.fetch_messages`.

The primitives are single-topic operations against the relay's HTTP API.
Callers that need to register N topics or paginate large streams loop.
Here we exercise the HTTP semantics against canned responses.
"""

import pytest
import responses
from pulsar_relay_client.topics import (
    HttpRelayClient,
    RefreshTokenRejectedError,
    RelayClientError,
    TopicOwnershipConflictError,
)

RELAY_URL = "https://relay.test"
TOPIC_NAME = "job_setup_byoc_7_lab"
ACCESS_TOKEN = "AT"


def _add_whoami(user_id: str = "u-1") -> None:
    responses.add(
        responses.GET,
        f"{RELAY_URL}/auth/me",
        json={"user_id": user_id, "username": "byoc_7_lab"},
        status=200,
    )


@responses.activate
def test_create_or_verify_topic_creates_new_topic():
    """Happy path: POST returns 201, no whoami needed."""
    responses.add(
        responses.POST,
        f"{RELAY_URL}/api/v1/topics",
        json={"topic_name": TOPIC_NAME, "owner_id": "u-1"},
        status=201,
    )

    HttpRelayClient(RELAY_URL).create_or_verify_topic(ACCESS_TOKEN, TOPIC_NAME)

    # Exactly one POST; no GET /auth/me on the happy path.
    assert [c.request.method for c in responses.calls] == ["POST"]


@responses.activate
def test_create_or_verify_topic_accepts_existing_when_owner_matches():
    """Re-registration path: topic exists, we own it — succeeds."""
    _add_whoami(user_id="u-1")
    responses.add(responses.POST, f"{RELAY_URL}/api/v1/topics", json={"detail": "exists"}, status=400)
    responses.add(responses.GET, f"{RELAY_URL}/api/v1/topics/{TOPIC_NAME}", json={"owner_id": "u-1"}, status=200)

    HttpRelayClient(RELAY_URL).create_or_verify_topic(ACCESS_TOKEN, TOPIC_NAME)


@responses.activate
def test_create_or_verify_topic_refuses_when_owned_by_other():
    """Cross-user defence: another principal pre-created the topic."""
    _add_whoami(user_id="u-1")
    responses.add(responses.POST, f"{RELAY_URL}/api/v1/topics", json={"detail": "exists"}, status=409)
    responses.add(responses.GET, f"{RELAY_URL}/api/v1/topics/{TOPIC_NAME}", json={"owner_id": "u-evil"}, status=200)

    with pytest.raises(TopicOwnershipConflictError, match="owned by another user"):
        HttpRelayClient(RELAY_URL).create_or_verify_topic(ACCESS_TOKEN, TOPIC_NAME)


@responses.activate
def test_create_or_verify_topic_refuses_when_existing_topic_unreadable():
    """Existing topic + GET fails (403/404) → assume foul play, refuse."""
    _add_whoami(user_id="u-1")
    responses.add(responses.POST, f"{RELAY_URL}/api/v1/topics", json={"detail": "exists"}, status=409)
    responses.add(responses.GET, f"{RELAY_URL}/api/v1/topics/{TOPIC_NAME}", json={"detail": "denied"}, status=403)

    with pytest.raises(TopicOwnershipConflictError, match="cannot read it"):
        HttpRelayClient(RELAY_URL).create_or_verify_topic(ACCESS_TOKEN, TOPIC_NAME)


@responses.activate
def test_create_or_verify_topic_propagates_unexpected_http_failure():
    """5xx on the POST: surface as RelayClientError."""
    responses.add(
        responses.POST,
        f"{RELAY_URL}/api/v1/topics",
        json={"detail": "relay is melting"},
        status=500,
    )

    with pytest.raises(RelayClientError, match="HTTP 500"):
        HttpRelayClient(RELAY_URL).create_or_verify_topic(ACCESS_TOKEN, TOPIC_NAME)


@responses.activate
def test_create_or_verify_topic_aborts_on_whoami_failure():
    """If POST conflicts and we can't read /auth/me, refuse."""
    responses.add(responses.POST, f"{RELAY_URL}/api/v1/topics", json={"detail": "exists"}, status=409)
    responses.add(responses.GET, f"{RELAY_URL}/auth/me", json={"detail": "denied"}, status=403)

    with pytest.raises(RelayClientError, match="/auth/me"):
        HttpRelayClient(RELAY_URL).create_or_verify_topic(ACCESS_TOKEN, TOPIC_NAME)


# --- fetch_messages ----------------------------------------------------------

CAPABILITIES_TOPIC = "pulsar_capabilities"
SAMPLE_PAGE = {
    "messages": [
        {
            "message_id": "1700000000-0",
            "topic": CAPABILITIES_TOPIC,
            "payload": {"schema_version": 1, "manager_name": "_default_"},
            "timestamp": "2026-05-13T14:33:07.412Z",
            "metadata": None,
        }
    ],
    "total": 1,
    "limit": 1,
    "order": "desc",
    "cursor": None,
    "next_cursor": None,
}


@responses.activate
def test_fetch_messages_returns_paginated_response_verbatim():
    """Happy path: 200 → response body returned as a dict (not just messages)."""
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json=SAMPLE_PAGE,
        status=200,
    )

    out = HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC, limit=1, order="desc")

    assert out == SAMPLE_PAGE
    # And the bearer token was carried.
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


@responses.activate
def test_fetch_messages_passes_limit_order_cursor_as_query_params():
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json={"messages": [], "total": 0, "limit": 5, "order": "asc", "cursor": "msg_42", "next_cursor": None},
        status=200,
    )

    HttpRelayClient(RELAY_URL).fetch_messages(
        ACCESS_TOKEN,
        CAPABILITIES_TOPIC,
        limit=5,
        order="asc",
        cursor="msg_42",
    )

    qs = responses.calls[0].request.url.split("?", 1)[1]
    # responses preserves order; assert each piece independently to be insensitive to encoding.
    assert "limit=5" in qs
    assert "order=asc" in qs
    assert "cursor=msg_42" in qs


@responses.activate
def test_fetch_messages_omits_cursor_when_none():
    """``cursor=None`` means "no cursor": don't send ``cursor=`` in the query
    string at all (the relay treats it as a sentinel for "first page")."""
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json=SAMPLE_PAGE,
        status=200,
    )

    HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC)

    qs = responses.calls[0].request.url.split("?", 1)[1]
    assert "cursor" not in qs


def test_fetch_messages_rejects_invalid_order():
    with pytest.raises(ValueError, match="order must be"):
        HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC, order="sideways")


@responses.activate
def test_fetch_messages_url_encodes_topic_name():
    """Topic names containing slashes / spaces / dots must be percent-encoded
    so they don't escape the ``/topics/{name}/messages`` path segment."""
    weird = "weird/../topic name"
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/weird%2F..%2Ftopic%20name/messages",
        json=SAMPLE_PAGE,
        status=200,
    )

    HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, weird)

    # The path segment is fully encoded — no raw '/' or space in the URL path.
    path = responses.calls[0].request.url.split("?", 1)[0]
    assert "weird%2F..%2Ftopic%20name" in path


@responses.activate
def test_fetch_messages_401_raises_refresh_token_rejected():
    """Bearer is rejected: caller refreshes via exchange_refresh_token."""
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json={"detail": "expired"},
        status=401,
    )

    with pytest.raises(RefreshTokenRejectedError):
        HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC)


@responses.activate
def test_fetch_messages_404_raises_relay_client_error():
    """Topic doesn't exist (caller's responsibility to interpret)."""
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json={"detail": "not found"},
        status=404,
    )

    with pytest.raises(RelayClientError, match="HTTP 404"):
        HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC)


@responses.activate
def test_fetch_messages_5xx_raises_relay_client_error():
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json={"detail": "boom"},
        status=503,
    )

    with pytest.raises(RelayClientError, match="HTTP 503"):
        HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC)


@responses.activate
def test_fetch_messages_non_json_body_raises():
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        body="not json",
        status=200,
        content_type="text/plain",
    )

    with pytest.raises(RelayClientError, match="non-JSON body"):
        HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC)


@responses.activate
def test_fetch_messages_empty_topic_returns_empty_messages_list():
    """Topic exists but has no messages yet → empty list, no exception."""
    empty = {"messages": [], "total": 0, "limit": 1, "order": "desc", "cursor": None, "next_cursor": None}
    responses.add(
        responses.GET,
        f"{RELAY_URL}/api/v1/topics/{CAPABILITIES_TOPIC}/messages",
        json=empty,
        status=200,
    )

    out = HttpRelayClient(RELAY_URL).fetch_messages(ACCESS_TOKEN, CAPABILITIES_TOPIC)
    assert out["messages"] == []
