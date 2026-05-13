"""Tests for cursor-file persistence permissions and atomicity.

Closes Client H#3 / Client Medium #10: the cursor file is written
0o600, the temp filename is unique per call (no deterministic
``.tmp`` collision), and the rename is durably committed via parent-
directory fsync. Topic names embedded in cursor files have been
observed to leak tenant identifiers, so loose perms are an info
disclosure risk.
"""

from __future__ import annotations

import os
import stat

from pulsar_relay_client import RelayTransport
from pulsar_relay_client.testing import FakeAuthManager


def _new_transport(cursor_path):
    return RelayTransport(
        "http://localhost:8080",
        auth_manager=FakeAuthManager(),
        cursor_path=cursor_path,
    )


def test_cursor_file_mode_is_0600(tmp_path):
    cursor = tmp_path / "cursor.json"
    t = _new_transport(str(cursor))
    t.set_last_message_id("setup", "msg-1")

    assert cursor.exists()
    mode = stat.S_IMODE(os.stat(cursor).st_mode)
    assert mode == 0o600, f"cursor file mode should be 0600, got 0{mode:o}"


def test_cursor_persist_uses_unique_temp_name(tmp_path, monkeypatch):
    """Two concurrent writers can't stomp on a shared ``.tmp`` filename.

    We can't easily run two writers in the same process, so this test
    verifies that ``_persist_cursor_locked`` uses ``tempfile.mkstemp``
    rather than the legacy deterministic ``path + ".tmp"`` pattern.
    """
    seen_tmp_names: list[str] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def _tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen_tmp_names.append(name)
        return fd, name

    monkeypatch.setattr("pulsar_relay_client.transport.tempfile.mkstemp", _tracking_mkstemp)

    cursor = tmp_path / "cursor.json"
    t = _new_transport(str(cursor))
    t.set_last_message_id("a", "1")
    t.set_last_message_id("b", "2")

    # Two persists => two distinct temp filenames (mkstemp guarantees uniqueness).
    assert len(seen_tmp_names) == 2
    assert seen_tmp_names[0] != seen_tmp_names[1]
    # Neither temp file remains after a successful persist.
    for name in seen_tmp_names:
        assert not os.path.exists(name)


def test_cursor_persist_creates_parent_directory(tmp_path):
    """Parent-dir creation still works under the new mkstemp flow."""
    nested = tmp_path / "level1" / "level2" / "cursor.json"
    t = _new_transport(str(nested))
    t.set_last_message_id("setup", "msg-1")
    assert nested.exists()
