"""Test-support helpers for consumers of ``pulsar-relay-client``.

Ships:

* :class:`FakeRelayClient` — in-memory implementation of the
  :class:`pulsar_relay_client.RelayClient` Protocol.
* :class:`FakeAuthManager` — drop-in stand-in for
  :class:`pulsar_relay_client.RelayAuthManager` that returns a canned
  access token without touching the network. Use it via the
  ``auth_manager=`` constructor parameter on :class:`RelayTransport`.

**These helpers bypass authentication.** They are safe in test
processes but disastrous if accidentally imported into a production
deployment — see Client Low #16 in the security review. Importing
this module emits a ``RuntimeWarning`` so a stray ``from
pulsar_relay_client.testing import ...`` in a config file shows up
in stderr at process start.
"""

from __future__ import annotations

import warnings
from typing import Any

from .topics import TopicOwnershipConflictError

warnings.warn(
    "pulsar_relay_client.testing was imported. Its FakeRelayClient and "
    "FakeAuthManager bypass authentication and must NEVER be used in a "
    "production process. Use them only from test code.",
    RuntimeWarning,
    stacklevel=2,
)


class FakeAuthManager:
    """Test double for :class:`pulsar_relay_client.RelayAuthManager`.

    Returns the configured token from ``get_token()`` without issuing any
    HTTP request; ``invalidate()`` is a no-op. Pass an instance via
    ``RelayTransport(..., auth_manager=FakeAuthManager())`` instead of
    monkey-patching ``RelayAuthManager`` at the module level.
    """

    def __init__(self, token: str = "stub-access-token") -> None:
        self.token = token
        self.invalidate_calls = 0

    def get_token(self) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidate_calls += 1


class FakeRelayClient:
    """In-memory :class:`pulsar_relay_client.RelayClient`.

    Backed by dicts the test owns; lets a test verify side effects
    without spinning up an HTTP server. The access token returned by
    :meth:`exchange_refresh_token` is opaque to this fake — pass a real
    JWT in via ``rotated_access_token`` if your code under test decodes
    its ``sub`` claim.
    """

    def __init__(
        self,
        user_id: str = "u-1",
        username: str = "fake-user",
        rotated_access_token: str = "AT-NEW",
        rotated_refresh_token: str = "RT-NEW",
    ) -> None:
        self.user_id = user_id
        self.username = username
        self._rotated_access_token = rotated_access_token
        self._rotated_refresh_token = rotated_refresh_token
        # topic_name -> owner_id (records which topics this fake created)
        self.created: dict[str, str] = {}
        # External pre-claims that should trigger a TopicOwnershipConflictError.
        self.preclaimed: dict[str, str] = {}
        self.exchange_calls: list[str] = []
        self.whoami_calls: list[str] = []

    def exchange_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        self.exchange_calls.append(refresh_token)
        return {
            "access_token": self._rotated_access_token,
            "refresh_token": self._rotated_refresh_token,
            "token_type": "bearer",
            "expires_in": 3600,
        }

    def whoami(self, access_token: str) -> dict[str, Any]:
        self.whoami_calls.append(access_token)
        return {"user_id": self.user_id, "username": self.username}

    def create_or_verify_topic(self, access_token: str, topic_name: str) -> None:
        existing = self.preclaimed.get(topic_name)
        if existing is not None and existing != self.user_id:
            raise TopicOwnershipConflictError(f"relay topic {topic_name} is owned by another user ({existing!r})")
        self.created[topic_name] = self.user_id


__all__ = ["FakeAuthManager", "FakeRelayClient"]
