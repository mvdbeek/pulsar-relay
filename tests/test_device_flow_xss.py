"""Tests for HTML-escaping on the device-flow approval page.

Closes Auth M#9 (stored-XSS on the device-approval page via
``client_hint``). The page was previously rendered with an f-string
that interpolated attacker-controlled values verbatim:

* ``record.client_hint`` — populated from the device-flow request body
  ``Client-Hint`` form field, OR (if absent) the ``User-Agent`` header.
  Both are attacker-controlled.
* ``cfg.display_name`` — operator-supplied, but routed through the same
  template so we escape it too.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from pulsar_relay.auth.dependencies import set_device_code_storage, set_oidc_clients
from pulsar_relay.auth.device_flow import InMemoryDeviceCodeStorage
from pulsar_relay.config import OIDCProviderConfig, settings
from pulsar_relay.main import app


@pytest.fixture
def device_storage_with_xss_payload(monkeypatch):
    """Seed the in-memory device-code storage with a record whose
    ``client_hint`` carries an XSS payload, and ensure at least one OIDC
    provider exists so the approval page actually renders its buttons.
    """
    storage = InMemoryDeviceCodeStorage()
    set_device_code_storage(storage)
    # The approval page is gated behind ``settings.oidc.enabled`` plus at
    # least one provider. Enable both for the duration of the test.
    monkeypatch.setattr(settings.oidc, "enabled", True)
    monkeypatch.setattr(settings.oidc, "base_url", "http://relay.test")
    monkeypatch.setitem(
        settings.oidc.providers,
        "keycloak",
        OIDCProviderConfig(
            display_name="<img src=x onerror=alert(2)>",  # also test display_name escaping
            client_id="test",
            client_secret="test",
            discovery_url="https://idp.test/.well-known/openid-configuration",
        ),
    )
    set_oidc_clients({})

    payload = "<script>alert(1)</script>"
    record, _ = asyncio.run(
        storage.create(
            verification_uri="http://relay/auth/device",
            verification_uri_complete_template="http://relay/auth/device?user_code={user_code}",
            ttl=timedelta(seconds=300),
            interval=5,
            client_hint=payload,
            pair=False,
        )
    )
    return record, payload


def test_device_approval_page_escapes_client_hint(device_storage_with_xss_payload):
    """The ``<script>`` payload must NOT appear unescaped in the HTML."""
    record, payload = device_storage_with_xss_payload

    client = TestClient(app)
    resp = client.get(f"/auth/device?user_code={record.user_code}")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # The raw payload must not appear. Either escaped or absent is fine.
    assert payload not in body, "client_hint rendered unescaped — XSS regression"
    # The escaped form should appear so the operator still sees what was
    # claimed.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_device_approval_page_escapes_provider_display_name(device_storage_with_xss_payload):
    """The provider display name is operator-supplied but goes through the
    same template; we escape it as defence-in-depth."""
    record, _ = device_storage_with_xss_payload

    client = TestClient(app)
    resp = client.get(f"/auth/device?user_code={record.user_code}")
    body = resp.text

    assert "<img src=x onerror=alert(2)>" not in body
    assert "&lt;img src=x onerror=alert(2)&gt;" in body
