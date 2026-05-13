"""Tests for ``pulsar_relay_client._url`` URL handling helpers.

Closes Client H#1 (path injection via unencoded topic names) and the
plaintext-http / userinfo / path-in-base-URL leak paths.
"""

from __future__ import annotations

import pytest
from pulsar_relay_client import HttpRelayClient, RelayAuthManager, RelayURLError
from pulsar_relay_client._url import normalize_relay_url, quote_topic


def test_normalize_strips_trailing_slash() -> None:
    assert normalize_relay_url("https://relay.example/") == "https://relay.example"


def test_normalize_preserves_https_with_port() -> None:
    assert normalize_relay_url("https://relay.example:9000") == "https://relay.example:9000"


def test_normalize_allows_http_localhost() -> None:
    assert normalize_relay_url("http://localhost:8080") == "http://localhost:8080"
    assert normalize_relay_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_normalize_rejects_http_to_remote_host() -> None:
    with pytest.raises(RelayURLError, match="plaintext"):
        normalize_relay_url("http://relay.example.org")


def test_normalize_rejects_userinfo() -> None:
    with pytest.raises(RelayURLError, match="username:password"):
        normalize_relay_url("https://alice:secret@relay.example.org")


def test_normalize_rejects_path_component() -> None:
    with pytest.raises(RelayURLError, match="path component"):
        normalize_relay_url("https://relay.example/api/v1")


def test_normalize_rejects_query_and_fragment() -> None:
    with pytest.raises(RelayURLError, match="query string or fragment"):
        normalize_relay_url("https://relay.example?token=abc")
    with pytest.raises(RelayURLError, match="query string or fragment"):
        normalize_relay_url("https://relay.example#frag")


def test_normalize_rejects_unknown_scheme() -> None:
    with pytest.raises(RelayURLError, match="http://"):
        normalize_relay_url("ftp://relay.example.org")


def test_http_relay_client_validates_relay_url() -> None:
    """Constructor surfaces RelayURLError instead of building a broken client."""
    with pytest.raises(RelayURLError):
        HttpRelayClient("http://relay.example.org")


def test_relay_auth_manager_validates_relay_url() -> None:
    with pytest.raises(RelayURLError):
        RelayAuthManager("http://relay.example.org", "user", "pw")


def test_quote_topic_encodes_path_traversal() -> None:
    """Closes Client H#1: a topic name containing ``../`` must not be
    able to escape the topics namespace."""
    encoded = quote_topic("../admin/users")
    assert "/" not in encoded
    assert ".." in encoded  # the dots themselves are fine; the slash is gone
    # And typical names round-trip unchanged.
    assert quote_topic("normal-topic_1") == "normal-topic_1"


def test_quote_topic_encodes_percent_and_hash() -> None:
    assert "%25" in quote_topic("with%percent")
    assert "%23" in quote_topic("with#hash")
