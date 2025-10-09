"""Tests for application configuration."""

import os
import pytest
from app.config import Settings


class TestSettings:
    """Tests for Settings class."""

    def test_default_settings(self):
        """Test that default settings are loaded correctly."""
        settings = Settings()

        assert settings.app_name == "Pulsar Proxy"
        assert settings.http_port == 8080
        assert settings.workers == 4
        assert settings.valkey_host == "localhost"
        assert settings.valkey_port == 6379
        assert settings.hot_tier_retention == 600
        assert settings.max_connections_per_instance == 10000

    def test_settings_from_env(self, monkeypatch):
        """Test that settings can be overridden by environment variables."""
        monkeypatch.setenv("HTTP_PORT", "9000")
        monkeypatch.setenv("VALKEY_HOST", "redis.example.com")
        monkeypatch.setenv("VALKEY_PORT", "7000")
        monkeypatch.setenv("MAX_CONNECTIONS_PER_INSTANCE", "20000")

        settings = Settings()

        assert settings.http_port == 9000
        assert settings.valkey_host == "redis.example.com"
        assert settings.valkey_port == 7000
        assert settings.max_connections_per_instance == 20000

    def test_case_insensitive_env_vars(self, monkeypatch):
        """Test that environment variables are case-insensitive."""
        monkeypatch.setenv("http_port", "8888")
        monkeypatch.setenv("VALKEY_HOST", "test.host")

        settings = Settings()

        assert settings.http_port == 8888
        assert settings.valkey_host == "test.host"
