"""Consumer-group wire contract tests."""

import responses
from pulsar_relay_client import RelayTransport
from pulsar_relay_client.testing import FakeAuthManager


def _transport() -> RelayTransport:
    return RelayTransport(
        "http://localhost:8080",
        auth_manager=FakeAuthManager(),
        sleep=lambda _: None,
    )


@responses.activate
def test_grouped_poll_attaches_delivery_without_advancing_cursor() -> None:
    responses.post(
        "http://localhost:8080/messages/poll",
        json={
            "messages": [
                {
                    "topic": "job-status",
                    "message_id": "1-0",
                    "payload": {"job_id": "42", "status": "complete"},
                }
            ],
            "has_more": False,
            "group": {
                "name": "galaxy-job-status-v1",
                "consumer": "handler-a:boot-1",
                "visibility_timeout": 300,
                "lease_expires_at": "2026-07-25T12:00:00Z",
                "deliveries": [{"topic": "job-status", "message_id": "1-0"}],
            },
        },
        status=200,
    )
    transport = _transport()

    messages = transport.long_poll(
        ["job-status"],
        group="galaxy-job-status-v1",
        consumer="handler-a:boot-1",
        max_messages=1,
    )

    assert messages[0]["_relay_delivery"] == {
        "group": "galaxy-job-status-v1",
        "consumer": "handler-a:boot-1",
        "topic": "job-status",
        "message_id": "1-0",
        "lease_expires_at": "2026-07-25T12:00:00Z",
    }
    assert transport.get_all_tracked_message_ids() == {}
    assert responses.calls[0].request.body is not None
    assert b'"group": "galaxy-job-status-v1"' in responses.calls[0].request.body


@responses.activate
def test_ack_and_touch_group_delivery() -> None:
    responses.post(
        "http://localhost:8080/messages/ack",
        json={"updated": 1},
        status=200,
    )
    responses.post(
        "http://localhost:8080/messages/ack",
        json={"updated": 0},
        status=200,
    )
    transport = _transport()
    delivery = {
        "group": "galaxy-job-status-v1",
        "consumer": "handler-a:boot-1",
        "topic": "job-status",
        "message_id": "1-0",
    }

    assert transport.update_group_delivery(delivery, "ack")
    assert not transport.update_group_delivery(delivery, "touch")
