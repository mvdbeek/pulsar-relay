"""Test-only helpers for resetting Valkey state between tests.

The production :class:`pulsar_relay.storage.valkey.ValkeyStorage` no longer
exposes a ``clear()`` method because ``FLUSHALL`` and ``FLUSHDB`` are renamed
(empty) in the hardened ``valkey.conf`` shipped with the relay. This module
provides a ``SCAN`` + ``DEL`` alternative that works against the hardened
instance.

Use :func:`reset_valkey_storage` from fixture setup/teardown when a test
needs a clean Valkey DB; use :func:`reset_valkey_client` when a test holds
its own bare :class:`glide.GlideClient` rather than going through
``ValkeyStorage``.

:func:`valkey_test_credentials` reads ``PULSAR_VALKEY_PASSWORD`` (set by
``tests/conftest.py`` or CI) so integration fixtures can authenticate
against a hardened (``--requirepass``-protected) Valkey instance.
"""

from __future__ import annotations

import os
from typing import Any

from pulsar_relay.storage.valkey import ValkeyStorage


def valkey_test_credentials() -> tuple[str | None, str | None]:
    """Return ``(username, password)`` from the test environment.

    When ``PULSAR_VALKEY_PASSWORD`` is set, username defaults to
    ``"default"`` (the implicit user that ``valkey-server --requirepass``
    configures). ``glide_shared`` only writes a non-empty username into
    the connection protobuf, so leaving it ``None`` makes the Rust core
    send ``HELLO ... AUTH "" <pw>`` — which Valkey 9 rejects with
    ``WRONGPASS`` even though ``redis-cli -a`` (legacy ``AUTH <pw>``)
    succeeds against the same instance. Setting it explicitly avoids
    that asymmetry.

    Password is ``None`` only if the env var is unset, which happens
    when running the suite against an unauthenticated Valkey.
    """
    password = os.environ.get("PULSAR_VALKEY_PASSWORD")
    username = os.environ.get("PULSAR_VALKEY_USERNAME") or ("default" if password else None)
    return username, password


async def reset_valkey_storage(storage: ValkeyStorage) -> None:
    """Delete every key in the connected Valkey instance via SCAN + DEL."""
    client = storage._client
    if client is None:
        raise RuntimeError("ValkeyStorage is not connected; cannot reset.")
    await _reset_via_scan(client)


async def reset_valkey_client(client: Any) -> None:
    """SCAN + DEL reset for a bare GlideClient."""
    await _reset_via_scan(client)


async def _reset_via_scan(client: Any) -> None:
    cursor: Any = b"0"
    while True:
        result = await client.scan(cursor, count=500)
        # GLIDE returns [next_cursor, keys]
        next_cursor, keys = result[0], result[1]
        if keys:
            await client.delete(keys)
        cursor = next_cursor
        if cursor in (b"0", "0", 0):
            break
