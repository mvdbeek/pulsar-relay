"""URL handling helpers shared by the client distribution.

Two small jobs:

* :func:`normalize_relay_url` validates and normalizes a relay base URL.
  Rejects ``http://`` outside localhost (the bearer JWT would travel
  unencrypted), rejects credentials embedded in the URL (``user:pw@``),
  and rejects path/query/fragment components — the relay's API paths
  are always appended by the client, so a base URL with its own
  ``/api`` prefix has historically led to broken URLs like
  ``…/api/v1/auth/login``.

* :func:`quote_topic` URL-encodes a topic name before it lands in an
  HTTP path segment. Topic names are constrained to ``[A-Za-z0-9_-]``
  upstream (see :data:`pulsar_relay.models.TopicName`) but never assume
  upstream validation: encoding here is defence-in-depth so a relaxed
  upstream contract or a future feature change can't introduce
  path-injection (e.g. ``../../admin/users``).
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlparse

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

INSECURE_BYPASS_ENV_VAR = "PULSAR_RELAY_ALLOW_INSECURE"


class RelayURLError(ValueError):
    """Raised when a relay base URL fails validation."""


def normalize_relay_url(url: str, *, allow_insecure_localhost: bool = True) -> str:
    """Validate and normalize a relay base URL.

    Returns the normalized form (``scheme://netloc``, no trailing
    slash) on success. Raises :class:`RelayURLError` with a precise
    reason on rejection.

    Acceptable: ``https://relay.example.org``, ``http://localhost:8080``
    (when ``allow_insecure_localhost``), ``https://relay.example.org:9000``.

    Rejected:
    * ``http://relay.example.org`` (bearer JWT in plaintext)
    * ``https://user:pw@relay.example.org`` (userinfo in URL leaks via
      Referer / access logs)
    * ``https://relay.example.org/api/v1`` (path component — the
      client appends its own paths; concatenation would double up)
    * ``https://relay.example.org?token=abc`` / ``#frag``

    Setting ``PULSAR_RELAY_ALLOW_INSECURE=1`` in the environment
    disables the plaintext-to-non-localhost rejection. Intended for
    test harnesses (e.g. fault-injection through a non-TLS proxy)
    where the operator has explicitly opted in to insecure transport;
    never enable it in production.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RelayURLError(f"relay_url must use http:// or https:// (got {parsed.scheme!r})")
    if not parsed.hostname:
        raise RelayURLError("relay_url is missing a host")
    if parsed.username is not None or parsed.password is not None:
        raise RelayURLError("relay_url must not embed username:password (use auth_manager instead)")
    if parsed.path not in ("", "/"):
        raise RelayURLError(
            f"relay_url must not include a path component (got {parsed.path!r}); "
            "the client appends API paths itself."
        )
    if parsed.query or parsed.fragment:
        raise RelayURLError("relay_url must not include a query string or fragment")
    if parsed.scheme == "http" and parsed.hostname not in _LOCALHOST_HOSTS:
        if os.environ.get(INSECURE_BYPASS_ENV_VAR) != "1":
            raise RelayURLError(
                f"refusing plaintext http:// to non-localhost host {parsed.hostname!r}; "
                f"use https:// (or set {INSECURE_BYPASS_ENV_VAR}=1 for test harnesses "
                "that explicitly opt in to insecure transport)."
            )
    if parsed.scheme == "http" and parsed.hostname in _LOCALHOST_HOSTS and not allow_insecure_localhost:
        raise RelayURLError(f"refusing plaintext http:// to {parsed.hostname!r} (allow_insecure_localhost=False)")
    # Normalize: drop any trailing slash, keep scheme + netloc.
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def quote_topic(name: str) -> str:
    """URL-encode a topic name for use in a path segment.

    ``safe=""`` means even ``/`` and ``%`` are encoded — required to
    prevent ``../../escape`` style path injection. Closes Client H#1.
    """
    return quote(name, safe="")


__all__ = ["RelayURLError", "normalize_relay_url", "quote_topic"]
