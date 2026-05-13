"""Tests for pulsar_relay_client.transport cursor persistence.

The long-poll cursor must survive a process restart so that messages published
to the relay while a consumer was down are NOT silently skipped on resume.
"""

import json
import os

import responses
from pulsar_relay_client import RelayTransport
from pulsar_relay_client.testing import FakeAuthManager


def _new_transport(cursor_path=None):
    return RelayTransport(
        "http://localhost:8080/",  # http only accepted for localhost; remote hosts must use https
        cursor_path=cursor_path,
        auth_manager=FakeAuthManager(),
    )


def test_cursor_persists_on_set(tmp_path):
    cursor = str(tmp_path / "cursor.json")
    t = _new_transport(cursor_path=cursor)
    t.set_last_message_id("setup", "msg-100")
    t.set_last_message_id("status", "msg-7")
    with open(cursor) as fh:
        on_disk = json.load(fh)
    assert on_disk == {"setup": "msg-100", "status": "msg-7"}


def test_cursor_loads_on_init(tmp_path):
    cursor = str(tmp_path / "cursor.json")
    with open(cursor, "w") as fh:
        json.dump({"setup": "msg-42", "kill": "msg-1"}, fh)
    t = _new_transport(cursor_path=cursor)
    assert t.get_last_message_id("setup") == "msg-42"
    assert t.get_last_message_id("kill") == "msg-1"
    assert t.get_last_message_id("never-seen") is None


def test_restart_simulation_resumes_from_disk(tmp_path):
    cursor = str(tmp_path / "cursor.json")
    t1 = _new_transport(cursor_path=cursor)
    t1.set_last_message_id("status_update", "msg-99")
    t1.close()

    t2 = _new_transport(cursor_path=cursor)
    assert t2.get_last_message_id("status_update") == "msg-99"


def test_cursor_atomic_write_does_not_leave_tmp_file(tmp_path):
    cursor = str(tmp_path / "cursor.json")
    t = _new_transport(cursor_path=cursor)
    t.set_last_message_id("setup", "msg-1")
    files = os.listdir(str(tmp_path))
    assert "cursor.json" in files
    assert not any(f.endswith(".tmp") for f in files)


def test_clear_persists(tmp_path):
    cursor = str(tmp_path / "cursor.json")
    t = _new_transport(cursor_path=cursor)
    t.set_last_message_id("a", "1")
    t.set_last_message_id("b", "2")
    t.clear_tracked_message_ids("a")
    with open(cursor) as fh:
        assert json.load(fh) == {"b": "2"}
    t.clear_tracked_message_ids()
    with open(cursor) as fh:
        assert json.load(fh) == {}


def test_no_cursor_path_means_no_persistence(tmp_path):
    t = _new_transport(cursor_path=None)
    t.set_last_message_id("setup", "msg-1")
    assert os.listdir(str(tmp_path)) == []


def test_corrupt_cursor_file_does_not_crash(tmp_path):
    cursor = str(tmp_path / "cursor.json")
    with open(cursor, "w") as fh:
        fh.write("{not json")
    t = _new_transport(cursor_path=cursor)
    assert t.get_all_tracked_message_ids() == {}


def test_cursor_directory_is_created(tmp_path):
    cursor = str(tmp_path / "nested" / "subdir" / "cursor.json")
    t = _new_transport(cursor_path=cursor)
    t.set_last_message_id("setup", "msg-1")
    assert os.path.exists(cursor)


@responses.activate
def test_long_poll_omits_replay_window_by_default():
    """Default ``replay_window_seconds=0`` keeps the existing wire shape —
    no extra field on the body, so a server pinned to an older relay
    release sees the request exactly as before."""
    captured = {}

    def _capture(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"messages": []}))

    responses.add_callback(
        responses.POST, "http://localhost:8080/messages/poll", callback=_capture, content_type="application/json"
    )
    _new_transport().long_poll(["setup"])
    assert "replay_window_seconds" not in captured["body"]


@responses.activate
def test_long_poll_passes_replay_window_when_set():
    """A non-zero window is forwarded verbatim."""
    captured = {}

    def _capture(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"messages": []}))

    responses.add_callback(
        responses.POST, "http://localhost:8080/messages/poll", callback=_capture, content_type="application/json"
    )
    _new_transport().long_poll(["setup"], replay_window_seconds=60)
    assert captured["body"]["replay_window_seconds"] == 60
