"""
JWT authentication manager for pulsar-relay.

The manager holds a short-lived access-token cache. The actual *acquire a
fresh token* policy is pluggable: PasswordAuthenticator (legacy), or
RefreshTokenAuthenticator (preferred, written by ``pulsar-config --login``).

A daemon's typical lifecycle is:

1. ``pulsar-config --login`` performs a one-time browser-based device-flow
   sign-in and writes ``relay_credentials.json`` containing a refresh token.
2. The daemon constructs a ``RelayAuthManager`` with a
   ``RefreshTokenAuthenticator`` pointing at that file.
3. The manager exchanges the refresh token for an access JWT on demand,
   atomically rewrites the credentials file with the rotated refresh token,
   and caches the access JWT until it nears expiry.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import cast

import requests

from ._url import normalize_relay_url
from .credentials import CredentialsFile, CredentialsStore, utcnow_iso

log = logging.getLogger(__name__)


class RelayAuthError(Exception):
    """Raised when the relay rejects an authentication attempt."""


class _Authenticator:
    """Strategy interface: produce a fresh ``(access_token, expires_in_seconds)``."""

    def authenticate(self) -> tuple[str, int]:
        raise NotImplementedError


class PasswordAuthenticator(_Authenticator):
    """Username/password against ``/auth/login`` (legacy path).

    If the relay returns a refresh token (new behavior), it is captured and
    written to the optional ``credentials_file`` so subsequent runs can use
    the refresh-token path.
    """

    def __init__(
        self,
        relay_url: str,
        username: str,
        password: str,
        *,
        credentials_file: CredentialsStore | None = None,
        timeout: int = 10,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.username = username
        self.password = password
        self._credentials_file = credentials_file
        self._timeout = timeout

    def authenticate(self) -> tuple[str, int]:
        url = f"{self.relay_url}/auth/login"
        try:
            resp = requests.post(
                url,
                data={
                    "username": self.username,
                    "password": self.password,
                    "grant_type": "password",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RelayAuthError(f"pulsar-relay password authentication failed: {exc}") from exc

        body = resp.json()
        access_token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))

        refresh_token = body.get("refresh_token")
        if refresh_token and self._credentials_file is not None:
            self._credentials_file.save(
                {
                    "relay_url": self.relay_url,
                    "refresh_token": refresh_token,
                    "issued_at": utcnow_iso(),
                }
            )
            log.info("Captured refresh token from /auth/login into %s", self._credentials_file.path)

        return access_token, expires_in


class RefreshTokenAuthenticator(_Authenticator):
    """Rotate a refresh token at ``/auth/token/refresh``.

    On success we atomically rewrite ``credentials_file`` so the next process
    inherits the rotated token. On 401 (revoked or expired) we leave the file
    in place and raise — the operator must re-run ``pulsar-config --login``.
    """

    def __init__(
        self,
        relay_url: str,
        credentials_file: CredentialsStore,
        *,
        timeout: int = 10,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self._credentials_file = credentials_file
        self._timeout = timeout

    def authenticate(self) -> tuple[str, int]:
        creds = self._credentials_file.load()
        if creds is None or not creds.get("refresh_token"):
            raise RelayAuthError(
                f"No refresh token at {self._credentials_file.path}; " "run `pulsar-config --login` to bootstrap one."
            )

        url = f"{self.relay_url}/auth/token/refresh"
        try:
            resp = requests.post(
                url,
                json={"refresh_token": creds["refresh_token"]},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise RelayAuthError(f"pulsar-relay refresh failed (network): {exc}") from exc

        if resp.status_code == 401:
            raise RelayAuthError("Refresh token rejected (revoked or expired). " "Re-run `pulsar-config --login`.")
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise RelayAuthError(f"pulsar-relay refresh failed: HTTP {resp.status_code}") from exc

        body = resp.json()
        access_token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        new_refresh = body.get("refresh_token")
        if new_refresh:
            # Persist the rotated refresh token. If this write fails we still
            # have a usable access token in hand for *this* process — but the
            # next process will choke. Surface the error.
            self._credentials_file.save(
                {
                    "relay_url": self.relay_url,
                    "refresh_token": new_refresh,
                    "issued_at": utcnow_iso(),
                }
            )
        return access_token, expires_in


class RelayAuthManager:
    """Thread-safe access-JWT cache backed by a pluggable authenticator.

    The narrow DI-friendly constructor takes ``(relay_url, authenticator)``.
    The legacy ``(relay_url, username, password, ...)`` signature is kept
    for back-compat by delegating to :func:`build_auth_manager`, which
    picks an authenticator from the supplied credentials.
    """

    def __init__(
        self,
        relay_url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        authenticator: _Authenticator | None = None,
        credentials_file: str | None = None,
        credentials_store: CredentialsStore | None = None,
    ) -> None:
        # Validates http://non-localhost / userinfo / path / query (see
        # ``_url.normalize_relay_url``). Raises RelayURLError on any of
        # those — the legacy ``rstrip("/")`` would have silently sent a
        # bearer JWT over plaintext.
        self.relay_url = normalize_relay_url(relay_url)
        self._token: str | None = None
        self._token_expiry: datetime | None = None
        self._lock = threading.Lock()
        # Refresh access JWT 5 minutes before expiry.
        self._refresh_buffer_seconds = 300

        if authenticator is None:
            authenticator = _select_authenticator(
                self.relay_url,
                username=username,
                password=password,
                credentials_file=credentials_file,
                credentials_store=credentials_store,
            )
        self._authenticator = authenticator

    # ---- public API ---------------------------------------------------------

    @property
    def strategy_name(self) -> str:
        """Name of the active authentication strategy.

        Stable, public alternative to inspecting the private
        ``_authenticator`` attribute. Returns ``"password"``,
        ``"refresh_token"``, or — for caller-supplied authenticators —
        the class's ``__name__``.
        """
        cls = type(self._authenticator)
        if cls is PasswordAuthenticator:
            return "password"
        if cls is RefreshTokenAuthenticator:
            return "refresh_token"
        return cls.__name__

    def get_token(self) -> str:
        with self._lock:
            if self._is_token_valid():
                return cast(str, self._token)
            log.debug("Fetching a fresh access token from pulsar-relay at %s", self.relay_url)
            access_token, expires_in = self._authenticator.authenticate()
            self._token = access_token
            self._token_expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)
            log.info("Acquired pulsar-relay access token (expires in %ds)", expires_in)
            return access_token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._token_expiry = None
            log.debug("Invalidated pulsar-relay access token cache")

    # ---- internals ----------------------------------------------------------

    def _is_token_valid(self) -> bool:
        if self._token is None or self._token_expiry is None:
            return False
        seconds_left = (self._token_expiry - datetime.now(tz=timezone.utc)).total_seconds()
        return seconds_left > self._refresh_buffer_seconds


def _select_authenticator(
    relay_url: str,
    *,
    username: str | None,
    password: str | None,
    credentials_file: str | None,
    credentials_store: CredentialsStore | None,
) -> _Authenticator:
    """Pick an authenticator from legacy-style construction inputs.

    ``credentials_store`` is the explicit, pre-built store path used by
    callers (e.g. the BYOC multi-tenant runner) that keep tokens in memory
    and persist rotations to their own vault. ``credentials_file`` is the
    legacy path string the daemon uses.
    """
    if credentials_store is not None:
        cred_store: CredentialsStore | None = credentials_store
    elif credentials_file is not None:
        cred_store = CredentialsFile(credentials_file)
    else:
        cred_store = None

    if cred_store is not None and cred_store.exists():
        return RefreshTokenAuthenticator(relay_url, cred_store)
    if username is not None and password is not None:
        return PasswordAuthenticator(relay_url, username, password, credentials_file=cred_store)
    raise ValueError("RelayAuthManager needs either an Authenticator, a credentials file/store, or username+password.")


def build_auth_manager(
    relay_url: str,
    *,
    authenticator: _Authenticator | None = None,
    username: str | None = None,
    password: str | None = None,
    credentials_file: str | None = None,
    credentials_store: CredentialsStore | None = None,
) -> RelayAuthManager:
    """Public factory that constructs a :class:`RelayAuthManager`.

    DI consumers (Galaxy, etc.) should prefer this over reaching for the
    legacy positional constructor — it makes the authenticator-selection
    branching explicit and keeps the narrow ``RelayAuthManager(url, authenticator=...)``
    constructor as the supported DI seam.
    """
    if authenticator is None:
        authenticator = _select_authenticator(
            relay_url.rstrip("/"),
            username=username,
            password=password,
            credentials_file=credentials_file,
            credentials_store=credentials_store,
        )
    return RelayAuthManager(relay_url, authenticator=authenticator)


__all__ = [
    "RelayAuthManager",
    "RelayAuthError",
    "PasswordAuthenticator",
    "RefreshTokenAuthenticator",
    "build_auth_manager",
]
