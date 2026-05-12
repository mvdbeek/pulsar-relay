"""Contract tests for :meth:`HttpRelayClient.create_or_verify_topic`.

The primitive is a single-topic create-or-verify-ownership dance against
the relay's HTTP API. Callers that need to register N topics (e.g.
Galaxy's BYOC manager) just loop. Here we exercise the HTTP semantics
against canned responses.
"""

import pytest
import responses
from pulsar_relay_client.topics import (
    HttpRelayClient,
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
