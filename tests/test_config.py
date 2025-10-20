"""Tests for application configuration."""

from app.config import Settings


class TestSettings:
    """Tests for Settings class."""

    def test_default_settings(self):
        """Test that default settings are loaded correctly."""
        settings = Settings()

        assert settings.app_name == "Pulsar Relay"
        assert settings.valkey_host == "localhost"
        assert settings.valkey_port == 6379
        assert settings.persistent_tier_retention == 86400
        assert settings.max_messages_per_topic == 1000000
        # Note: http_port and workers removed - controlled by uvicorn CLI

    def test_settings_from_env(self, monkeypatch):
        """Test that settings can be overridden by environment variables."""
        monkeypatch.setenv("PULSAR_VALKEY_HOST", "redis.example.com")
        monkeypatch.setenv("PULSAR_VALKEY_PORT", "7000")
        monkeypatch.setenv("PULSAR_MAX_MESSAGES_PER_TOPIC", "500000")

        settings = Settings()

        assert settings.valkey_host == "redis.example.com"
        assert settings.valkey_port == 7000
        assert settings.max_messages_per_topic == 500000
        # Note: http_port removed - controlled by uvicorn CLI

    def test_case_insensitive_env_vars(self, monkeypatch):
        """Test that environment variables are case-insensitive."""
        monkeypatch.setenv("pulsar_valkey_host", "test.host")
        monkeypatch.setenv("PULSAR_VALKEY_PORT", "7777")

        settings = Settings()

        assert settings.valkey_host == "test.host"
        assert settings.valkey_port == 7777
        # Note: http_port removed - controlled by uvicorn CLI
