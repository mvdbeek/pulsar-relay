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
"""

from __future__ import annotations

from typing import Any

from pulsar_relay.storage.valkey import ValkeyStorage


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
