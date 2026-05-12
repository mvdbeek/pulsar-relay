"""HTTP client for the relay's topic-management + token endpoints.

The create-or-verify-ownership dance is a pure relay-contract primitive
(POST ``/api/v1/topics``; on 400/409 GET ``/api/v1/topics/<name>`` and
compare ``owner_id``). Topic *naming* conventions — e.g. BYOC's
``job_setup_<manager>`` triple — belong to the caller, not the relay.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol, cast, runtime_checkable

import requests

log = logging.getLogger(__name__)


class RelayClientError(Exception):
    """Top-level relay-side failure surfaced to the caller."""


class RefreshTokenRejectedError(RelayClientError):
    """The relay returned 401 — the refresh token is revoked or expired."""


class TopicOwnershipConflictError(RelayClientError):
    """A topic with the requested name exists but is owned by another user.

    A clean signal that another principal has pre-emptively claimed the
    topic name. Callers translate this to their own domain-level failure
    mode (e.g. abort BYOC registration).
    """


@runtime_checkable
class RelayClient(Protocol):
    """The narrow surface a relay-aware caller needs from the client.

    Production code uses this as a type annotation when accepting a
    dependency-injected client (so tests can pass fakes). The concrete
    implementation is :class:`HttpRelayClient`.
    """

    def exchange_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """``POST /auth/token/refresh`` — returns the body with rotated tokens.

        Raises :class:`RefreshTokenRejectedError` on 401; :class:`RelayClientError`
        on any other failure.
        """

    def whoami(self, access_token: str) -> dict[str, Any]:
        """``GET /auth/me`` — returns the relay user record for the bearer.

        Raises :class:`RelayClientError` on any failure.
        """

    def create_or_verify_topic(self, access_token: str, topic_name: str) -> None:
        """Create ``topic_name``; on already-exists, verify the bearer owns it.

        Idempotent: re-running with the same caller is a no-op. Raises
        :class:`TopicOwnershipConflictError` if the topic exists but is owned by
        another user; :class:`RelayClientError` on transport or unexpected
        HTTP failures.
        """


class HttpRelayClient:
    """Production HTTP client for the relay's token + topic endpoints.

    A single instance is bound to one relay base URL. A
    :class:`requests.Session` is held internally; embedders that need to
    tune CA bundles, retry adapters, proxies, or instrumentation can pass
    a pre-configured one via the ``session`` parameter.
    """

    def __init__(
        self,
        relay_url: str,
        *,
        timeout: int = 10,
        session: requests.Session | None = None,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()

    # ---- token refresh ---------------------------------------------------

    def exchange_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        url = f"{self.relay_url}/auth/token/refresh"
        try:
            resp = self._session.post(url, json={"refresh_token": refresh_token}, timeout=self._timeout)
        except requests.RequestException as exc:
            raise RelayClientError(f"relay refresh failed (network): {exc}") from exc
        if resp.status_code == 401:
            raise RefreshTokenRejectedError("relay rejected refresh token (revoked or expired)")
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RelayClientError(f"relay refresh failed: HTTP {resp.status_code}") from exc
        try:
            return cast(dict[str, Any], resp.json())
        except ValueError as exc:
            raise RelayClientError(f"relay returned non-JSON body: {exc}") from exc

    # ---- /auth/me --------------------------------------------------------

    def whoami(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            resp = self._session.get(f"{self.relay_url}/auth/me", headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())
        except (requests.RequestException, ValueError) as exc:
            raise RelayClientError(f"could not read /auth/me on relay: {exc}") from exc

    # ---- topic create-or-verify -----------------------------------------

    def create_or_verify_topic(self, access_token: str, topic_name: str) -> None:
        """Create ``topic_name``; on already-exists, verify the bearer owns it.

        Idempotent: re-running with the same bearer is a no-op. Raises
        :class:`TopicOwnershipConflictError` if the topic exists but is
        owned by another user. The whoami lookup is deferred until we
        actually need it (POST returns 400/409), so the happy path is a
        single POST.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            resp = self._session.post(
                f"{self.relay_url}/api/v1/topics",
                headers={**headers, "Content-Type": "application/json"},
                json={"topic_name": topic_name},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise RelayClientError(f"relay topic POST {topic_name} failed (network): {exc}") from exc
        if resp.status_code in (200, 201):
            return
        if resp.status_code in (400, 409):
            my_user_id = self.whoami(access_token).get("user_id")
            if not my_user_id:
                raise RelayClientError("relay /auth/me returned no user_id")
            self._verify_existing_topic_owner(headers, topic_name, my_user_id)
            return
        raise RelayClientError(f"relay topic POST {topic_name} failed: HTTP {resp.status_code}")

    def _verify_existing_topic_owner(self, headers: dict[str, str], topic_name: str, expected_owner_id: str) -> None:
        check = self._session.get(
            f"{self.relay_url}/api/v1/topics/{topic_name}", headers=headers, timeout=self._timeout
        )
        if check.status_code != 200:
            raise TopicOwnershipConflictError(
                f"relay topic {topic_name} already exists and we cannot read it (HTTP {check.status_code})"
            )
        try:
            owner_id = check.json().get("owner_id")
        except ValueError as exc:
            raise RelayClientError(f"relay topic {topic_name} GET returned non-JSON: {exc}") from exc
        if owner_id != expected_owner_id:
            raise TopicOwnershipConflictError(f"relay topic {topic_name} is owned by another user ({owner_id!r})")


#: Factory signature: takes a relay base URL, returns a ``RelayClient``.
#: Typed against the :class:`RelayClient` Protocol (not the concrete
#: :class:`HttpRelayClient`) so DI consumers can inject any conforming
#: implementation — e.g. the in-memory ``FakeRelayClient`` from
#: :mod:`pulsar_relay_client.testing`.
RelayClientFactory = Callable[[str], "RelayClient"]


def default_relay_client_factory(relay_url: str) -> RelayClient:
    """Default factory used by consumers (e.g. Galaxy's BYOC manager) when
    no override is injected. Tests pass a different factory returning fakes.
    """
    return HttpRelayClient(relay_url)


__all__ = [
    "HttpRelayClient",
    "RelayClient",
    "RelayClientError",
    "RefreshTokenRejectedError",
    "TopicOwnershipConflictError",
    "RelayClientFactory",
    "default_relay_client_factory",
]
