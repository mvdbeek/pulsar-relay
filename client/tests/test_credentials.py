"""Tests for pulsar_relay_client.credentials.

The credentials store is the single source of refresh-token persistence,
so the atomic-write, mode-0600 and in-memory-rotation semantics matter.
"""

import os
import stat

from pulsar_relay_client.credentials import (
    CredentialsFile,
    InMemoryCredentialsStore,
    utcnow_iso,
)


def test_credentials_file_round_trip(tmp_path):
    cred = CredentialsFile(str(tmp_path / "rel.json"))
    assert not cred.exists()
    assert cred.load() is None
    cred.save({"relay_url": "https://r", "refresh_token": "JTI.SEC"})
    assert cred.exists()
    loaded = cred.load()
    assert loaded["refresh_token"] == "JTI.SEC"


def test_credentials_file_save_is_mode_0600(tmp_path):
    path = str(tmp_path / "rel.json")
    cred = CredentialsFile(path)
    cred.save({"refresh_token": "x"})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_credentials_file_save_overwrites_atomically(tmp_path):
    """An interrupted second save must never leave a half-written file."""
    path = str(tmp_path / "rel.json")
    cred = CredentialsFile(path)
    cred.save({"refresh_token": "v1"})
    cred.save({"refresh_token": "v2"})
    assert cred.load()["refresh_token"] == "v2"
    # No leftover temp files in the directory.
    leftovers = [f for f in os.listdir(str(tmp_path)) if f.startswith(".pulsar-relay-cred-")]
    assert leftovers == []


def test_credentials_file_load_returns_none_on_corrupt_json(tmp_path):
    path = str(tmp_path / "rel.json")
    with open(path, "w") as f:
        f.write("{not json")
    os.chmod(path, 0o600)
    cred = CredentialsFile(path)
    assert cred.load() is None


def test_credentials_file_load_refuses_symlink(tmp_path):
    """O_NOFOLLOW: loading via a symlink must NOT dereference. Closes
    Storage H#5 / Client H#5 — an attacker who can write into the
    parent dir could plant a symlink at the credentials path pointing
    at any file readable by the relay user."""
    real = tmp_path / "real_secret.json"
    real.write_text('{"refresh_token": "victim-secret"}')
    os.chmod(real, 0o600)

    link = tmp_path / "cred.json"
    os.symlink(str(real), str(link))

    cred = CredentialsFile(str(link))
    # load() must return None (refused) rather than the linked file's contents.
    assert cred.load() is None


def test_credentials_file_load_refuses_loose_parent(tmp_path):
    """Parent-dir permission check: refuse to load when the parent
    directory is group- or world-writable."""
    path = tmp_path / "rel.json"
    cred = CredentialsFile(str(path))
    cred.save({"refresh_token": "JTI.SEC"})

    # Loosen the parent dir.
    os.chmod(str(tmp_path), 0o777)
    try:
        assert cred.load() is None
    finally:
        os.chmod(str(tmp_path), 0o700)


def test_credentials_file_save_refuses_loose_parent(tmp_path):
    """Symmetric: refuse to WRITE to a credentials path inside a
    world-writable directory."""
    cred = CredentialsFile(str(tmp_path / "rel.json"))
    os.chmod(str(tmp_path), 0o777)
    try:
        import pytest

        with pytest.raises(OSError, match="world-writable"):
            cred.save({"refresh_token": "x"})
    finally:
        os.chmod(str(tmp_path), 0o700)


def test_in_memory_store_returns_copies(tmp_path):
    store = InMemoryCredentialsStore(relay_url="https://r", refresh_token="JTI.SEC")
    loaded = store.load()
    loaded["refresh_token"] = "MUTATED"
    # Internal state is decoupled from the returned dict.
    assert store.load()["refresh_token"] == "JTI.SEC"


def test_in_memory_store_fires_on_save_callback():
    saved = []
    store = InMemoryCredentialsStore(
        relay_url="https://r",
        refresh_token="OLD",
        on_save=saved.append,
    )
    store.save({"refresh_token": "NEW", "relay_url": "https://r"})
    assert len(saved) == 1
    assert saved[0]["refresh_token"] == "NEW"
    assert store.load()["refresh_token"] == "NEW"


def test_in_memory_store_swallows_callback_failure_but_keeps_rotation():
    def boom(_data):
        raise RuntimeError("vault down")

    store = InMemoryCredentialsStore(
        relay_url="https://r",
        refresh_token="OLD",
        on_save=boom,
    )
    # Must not raise — the callback failed but the new token is in memory.
    store.save({"refresh_token": "NEW", "relay_url": "https://r"})
    assert store.load()["refresh_token"] == "NEW"


def test_utcnow_iso_is_timezone_aware():
    assert "+" in utcnow_iso() or "Z" in utcnow_iso()
