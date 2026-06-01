"""Tests for optional Sentry error reporting.

These exercise the import-time ``_init_sentry`` helper and the explicit
``capture_exception`` call in the global exception handler, without any
network access.
"""

import builtins
import logging
from types import SimpleNamespace

import pulsar_relay.main as main


def _config(**overrides):
    """Build a settings-like object with the Sentry fields we care about."""
    base = {
        "sentry_dsn": None,
        "sentry_environment": None,
        "sentry_traces_sample_rate": 0.0,
        "sentry_send_default_pii": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_init_sentry_disabled_when_dsn_unset(monkeypatch):
    """No DSN -> returns None and never imports/initializes sentry_sdk."""
    called = False

    def fake_init(*args, **kwargs):
        nonlocal called
        called = True

    # Even if sentry_sdk is importable, init must not run without a DSN.
    monkeypatch.setattr("sentry_sdk.init", fake_init)

    assert main._init_sentry(_config(sentry_dsn=None)) is None
    assert called is False


def test_init_sentry_initializes_with_expected_kwargs(monkeypatch):
    """DSN set + sentry-sdk available -> init called with mapped settings."""
    captured = {}

    def fake_init(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("sentry_sdk.init", fake_init)

    config = _config(
        sentry_dsn="https://public@example.com/1",
        sentry_environment="staging",
        sentry_traces_sample_rate=0.25,
        sentry_send_default_pii=True,
    )
    result = main._init_sentry(config)

    import sentry_sdk

    assert result is sentry_sdk
    assert captured == {
        "dsn": "https://public@example.com/1",
        "environment": "staging",
        "traces_sample_rate": 0.25,
        "send_default_pii": True,
    }


def test_init_sentry_warns_when_sdk_missing(monkeypatch, caplog):
    """DSN set but sentry-sdk not installed -> warn, return None, no crash."""
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("no sentry_sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with caplog.at_level(logging.WARNING):
        result = main._init_sentry(_config(sentry_dsn="https://public@example.com/1"))

    assert result is None
    assert any("sentry-sdk is not installed" in r.message for r in caplog.records)


async def test_global_handler_captures_when_sentry_active(monkeypatch):
    """The handler reports to Sentry when reporting is active."""
    captured = []
    fake_sdk = SimpleNamespace(capture_exception=lambda exc: captured.append(exc))
    monkeypatch.setattr(main, "_sentry_sdk", fake_sdk)

    request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/boom"))
    exc = ValueError("kaboom")
    response = await main.global_exception_handler(request, exc)

    assert response.status_code == 500
    assert captured == [exc]


async def test_global_handler_noop_when_sentry_inactive(monkeypatch):
    """The handler must not touch Sentry when reporting is disabled."""
    monkeypatch.setattr(main, "_sentry_sdk", None)

    request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/boom"))
    response = await main.global_exception_handler(request, ValueError("kaboom"))

    assert response.status_code == 500
