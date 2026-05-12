"""Tests for the startup-secrets guard.

:func:`pulsar_relay.config.validate_startup_secrets` is the boot-time gate
that refuses to launch the relay with insecure defaults. The guard is the
load-bearing fix for security review Critical findings C1 (default JWT
secret) and C5 (missing Valkey AUTH); these tests assert each failure mode
raises and that the escape hatch works for local-dev / CI.

The :class:`Settings` model picks up ``PULSAR_*`` env vars at construction,
and the conftest sets a strong JWT secret + bootstrap admin password for the
test session. Each test therefore uses ``monkeypatch.setenv`` /
``monkeypatch.delenv`` to set the *exact* environment it wants tested,
rather than passing kwargs (which env-source priority overrides).
"""

from __future__ import annotations

import pytest

from pulsar_relay.config import (
    _DEFAULT_JWT_SECRET,
    InsecureDefaultsError,
    Settings,
    validate_startup_secrets,
)

_STRONG_JWT = "x" * 64
_STRONG_ADMIN_PW = "strong-bootstrap-admin-password"
_STRONG_VALKEY_PW = "strong-valkey-password"
_GOOD_ORIGINS = '["https://relay.example.com"]'
_GOOD_HOSTS = '["relay.example.com"]'


@pytest.fixture
def good_env(monkeypatch) -> None:
    """Set env vars matching a hardened production deployment."""
    monkeypatch.setenv("PULSAR_JWT_SECRET_KEY", _STRONG_JWT)
    monkeypatch.setenv("PULSAR_BOOTSTRAP_ADMIN_PASSWORD", _STRONG_ADMIN_PW)
    monkeypatch.setenv("PULSAR_STORAGE_BACKEND", "valkey")
    monkeypatch.setenv("PULSAR_VALKEY_PASSWORD", _STRONG_VALKEY_PW)
    monkeypatch.setenv("PULSAR_ALLOWED_ORIGINS", _GOOD_ORIGINS)
    monkeypatch.setenv("PULSAR_TRUSTED_HOSTS", _GOOD_HOSTS)
    monkeypatch.delenv("PULSAR_ALLOW_INSECURE_DEFAULTS", raising=False)


def test_passes_with_strong_secrets(good_env) -> None:
    """Happy path: every required field is non-default and >= 32 chars."""
    validate_startup_secrets(Settings())


def test_rejects_default_jwt_secret(good_env, monkeypatch) -> None:
    monkeypatch.setenv("PULSAR_JWT_SECRET_KEY", _DEFAULT_JWT_SECRET)
    with pytest.raises(InsecureDefaultsError, match="default value"):
        validate_startup_secrets(Settings())


def test_rejects_short_jwt_secret(good_env, monkeypatch) -> None:
    monkeypatch.setenv("PULSAR_JWT_SECRET_KEY", "too-short")
    with pytest.raises(InsecureDefaultsError, match="shorter than"):
        validate_startup_secrets(Settings())


def test_rejects_missing_bootstrap_admin_password(good_env, monkeypatch) -> None:
    monkeypatch.delenv("PULSAR_BOOTSTRAP_ADMIN_PASSWORD")
    with pytest.raises(InsecureDefaultsError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        validate_startup_secrets(Settings())


def test_rejects_missing_valkey_password_when_valkey_backend(good_env, monkeypatch) -> None:
    monkeypatch.delenv("PULSAR_VALKEY_PASSWORD")
    with pytest.raises(InsecureDefaultsError, match="VALKEY_PASSWORD"):
        validate_startup_secrets(Settings())


def test_allows_missing_valkey_password_when_memory_backend(good_env, monkeypatch) -> None:
    """The Valkey-password requirement only applies to the Valkey backend."""
    monkeypatch.setenv("PULSAR_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("PULSAR_VALKEY_PASSWORD")
    validate_startup_secrets(Settings())


def test_rejects_empty_allowed_origins(good_env, monkeypatch) -> None:
    monkeypatch.setenv("PULSAR_ALLOWED_ORIGINS", "[]")
    with pytest.raises(InsecureDefaultsError, match="ALLOWED_ORIGINS"):
        validate_startup_secrets(Settings())


def test_rejects_empty_trusted_hosts(good_env, monkeypatch) -> None:
    monkeypatch.setenv("PULSAR_TRUSTED_HOSTS", "[]")
    with pytest.raises(InsecureDefaultsError, match="TRUSTED_HOSTS"):
        validate_startup_secrets(Settings())


def test_escape_hatch_bypasses_all_checks(monkeypatch) -> None:
    """``PULSAR_ALLOW_INSECURE_DEFAULTS=1`` bypasses every guard branch.

    Used by the local-dev compose so contributors don't have to generate
    real secrets to run the stack. Never set in production deployments.
    """
    monkeypatch.setenv("PULSAR_JWT_SECRET_KEY", _DEFAULT_JWT_SECRET)
    monkeypatch.delenv("PULSAR_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("PULSAR_VALKEY_PASSWORD", raising=False)
    monkeypatch.setenv("PULSAR_ALLOW_INSECURE_DEFAULTS", "1")
    validate_startup_secrets(Settings())


def test_insecure_defaults_error_exits_nonzero() -> None:
    """``InsecureDefaultsError`` is a SystemExit subclass so an unhandled
    raise propagates as a non-zero process exit (code 2) — same behaviour
    operators expect when ``uvicorn`` aborts on a startup error."""
    err = InsecureDefaultsError("test")
    assert isinstance(err, SystemExit)
    assert err.code == 2
