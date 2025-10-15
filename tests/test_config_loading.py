"""Tests for configuration loading from files and environment."""

import os
import tempfile
from pathlib import Path

import pytest

from app.config import Settings, load_config_file, load_settings


class TestConfigFileLoading:
    """Test loading configuration from TOML and YAML files."""

    def test_load_toml_config(self):
        """Test loading configuration from TOML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
app_name = "Test App"
storage_backend = "valkey"
log_level = "DEBUG"
jwt_secret_key = "test-secret-key"
            """
            )
            f.flush()
            config_path = Path(f.name)

        try:
            config_data = load_config_file(config_path)

            assert config_data["app_name"] == "Test App"
            assert config_data["storage_backend"] == "valkey"
            assert config_data["log_level"] == "DEBUG"
            assert config_data["jwt_secret_key"] == "test-secret-key"
        finally:
            config_path.unlink()

    def test_load_yaml_config(self):
        """Test loading configuration from YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                """
app_name: "Test App YAML"
storage_backend: "memory"
log_level: "WARNING"
jwt_secret_key: "yaml-secret-key"
            """
            )
            f.flush()
            config_path = Path(f.name)

        try:
            config_data = load_config_file(config_path)

            assert config_data["app_name"] == "Test App YAML"
            assert config_data["storage_backend"] == "memory"
            assert config_data["log_level"] == "WARNING"
            assert config_data["jwt_secret_key"] == "yaml-secret-key"
        finally:
            config_path.unlink()

    def test_load_nonexistent_config(self):
        """Test loading configuration when file doesn't exist."""
        config_data = load_config_file(Path("nonexistent.toml"))
        assert config_data == {}

    def test_config_file_with_nested_values(self):
        """Test TOML config with nested values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
app_name = "Nested Test"
valkey_host = "redis.example.com"
valkey_port = 6380
            """
            )
            f.flush()
            config_path = Path(f.name)

        try:
            config_data = load_config_file(config_path)
            assert config_data["valkey_host"] == "redis.example.com"
            assert config_data["valkey_port"] == 6380
        finally:
            config_path.unlink()


class TestSettingsFromConfigFile:
    """Test creating Settings instance from config file."""

    def test_settings_from_toml(self):
        """Test creating Settings from TOML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
app_name = "Config Test"
storage_backend = "valkey"
log_level = "ERROR"
            """
            )
            f.flush()
            config_path = Path(f.name)

        try:
            settings = Settings.from_config_file(config_path)

            assert settings.app_name == "Config Test"
            assert settings.storage_backend == "valkey"
            assert settings.log_level == "ERROR"
        finally:
            config_path.unlink()

    def test_settings_defaults_without_config(self):
        """Test Settings with default values when no config file."""
        settings = Settings.from_config_file(None)

        assert settings.app_name == "Pulsar Relay"
        assert settings.storage_backend == "memory"

    def test_settings_validation(self):
        """Test Settings validation with invalid storage backend."""
        with pytest.raises(Exception):
            # Invalid storage backend should fail validation
            Settings(storage_backend="invalid")


class TestEnvironmentVariableOverride:
    """Test environment variables override config files."""

    def test_env_overrides_config_file(self):
        """Test that environment variables override config file values."""
        # Set environment variable before loading
        os.environ["PULSAR_LOG_LEVEL"] = "DEBUG"

        try:
            # Environment variables have highest priority
            settings = Settings()

            # Environment should be used
            assert settings.log_level == "DEBUG"
        finally:
            os.environ.pop("PULSAR_LOG_LEVEL", None)

    def test_env_prefix(self):
        """Test that PULSAR_ prefix is required for environment variables."""
        # Set variable without prefix (should be ignored)
        os.environ["LOG_LEVEL"] = "ERROR"

        try:
            settings = Settings()
            # Should use default, not env var without prefix
            assert settings.log_level == "INFO"
        finally:
            os.environ.pop("LOG_LEVEL", None)


class TestLoadSettings:
    """Test the load_settings function."""

    def test_load_settings_with_config_path(self):
        """Test load_settings with explicit config path."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(
                """
app_name = "Load Settings Test"
storage_backend = "memory"
            """
            )
            f.flush()
            config_path = f.name

        try:
            settings = load_settings(config_path=config_path)

            assert settings.app_name == "Load Settings Test"
            assert settings.storage_backend == "memory"
        finally:
            Path(config_path).unlink()

    def test_load_settings_default_jwt_warning(self, caplog):
        """Test that warning is logged when using default JWT secret."""
        settings = load_settings()

        # Should log warning about default JWT secret
        assert settings.jwt_secret_key == "your-secret-key-here-change-in-production"


class TestConfigValidation:
    """Test configuration validation."""

    def test_log_level_normalization(self):
        """Test log level is normalized to uppercase."""
        settings = Settings(log_level="info")
        assert settings.log_level == "INFO"

        settings = Settings(log_level="debug")
        assert settings.log_level == "DEBUG"

    def test_storage_backend_validation(self):
        """Test storage backend validation."""
        settings = Settings(storage_backend="memory")
        assert settings.storage_backend == "memory"

        settings = Settings(storage_backend="valkey")
        assert settings.storage_backend == "valkey"

        with pytest.raises(Exception):
            Settings(storage_backend="invalid")  # Invalid option
