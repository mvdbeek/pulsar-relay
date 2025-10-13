"""Application configuration with support for config files and environment variables.

Configuration loading precedence (highest to lowest):
1. Environment variables (highest priority)
2. Config file (config.toml or config.yaml)
3. Default values (lowest priority)
"""

import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional

import tomli
import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def load_config_file(config_path: Optional[Path] = None) -> dict[str, Any]:
    """Load configuration from TOML or YAML file.

    Args:
        config_path: Optional path to config file. If None, searches for
                    config.toml or config.yaml in current directory.

    Returns:
        Dictionary with configuration values
    """
    config_data: dict[str, Any] = {}

    # If no path specified, search for config files
    if config_path is None:
        possible_files = [
            Path("config.toml"),
            Path("config.yaml"),
            Path("config.yml"),
            Path("/etc/pulsar-proxy/config.toml"),
            Path("/etc/pulsar-proxy/config.yaml"),
        ]

        for file_path in possible_files:
            if file_path.exists():
                config_path = file_path
                logger.info(f"Found configuration file: {config_path}")
                break

    # Load config file if found
    if config_path and config_path.exists():
        try:
            with open(config_path, "rb" if config_path.suffix == ".toml" else "r") as f:
                if config_path.suffix == ".toml":
                    config_data = tomli.load(f)
                    logger.info(f"Loaded configuration from TOML: {config_path}")
                elif config_path.suffix in [".yaml", ".yml"]:
                    config_data = yaml.safe_load(f) or {}
                    logger.info(f"Loaded configuration from YAML: {config_path}")
        except Exception as e:
            logger.error(f"Error loading config file {config_path}: {e}")
            raise

    return config_data


class Settings(BaseSettings):
    """Application settings with support for config files and environment variables.

    Configuration loading order (highest to lowest priority):
    1. Environment variables (e.g. PULSAR_STORAGE_BACKEND=valkey)
    2. Config file (config.toml or config.yaml)
    3. Default values
    """

    # Server Configuration
    app_name: str = Field(
        default="Pulsar Proxy",
        description="Application name",
    )

    # Storage Backend Selection
    storage_backend: Literal["memory", "valkey"] = Field(
        default="memory",
        description="Storage backend to use (memory or valkey)",
    )

    # Valkey Configuration
    valkey_host: str = Field(
        default="localhost",
        description="Valkey server host",
    )
    valkey_port: int = Field(
        default=6379,
        description="Valkey server port",
        ge=1,
        le=65535,
    )
    valkey_password: str = Field(
        default="",
        description="Valkey password (empty for no auth)",
    )
    valkey_use_tls: bool = Field(
        default=False,
        description="Use TLS for Valkey connection",
    )

    # Storage Configuration
    hot_tier_retention: int = Field(
        default=600,
        description="Hot tier retention in seconds (in-memory cache)",
        ge=1,
    )
    persistent_tier_retention: int = Field(
        default=86400,
        description="Persistent tier retention in seconds (Valkey)",
        ge=1,
    )
    max_messages_per_topic: int = Field(
        default=1000000,
        description="Maximum messages to store per topic",
        ge=1,
    )

    # Limits
    max_connections_per_instance: int = Field(
        default=10000,
        description="Maximum WebSocket connections per instance",
        ge=1,
    )
    max_message_size: int = Field(
        default=1048576,
        description="Maximum message size in bytes (1MB)",
        ge=1024,
    )
    rate_limit_per_client: int = Field(
        default=1000,
        description="Rate limit per client (messages per minute)",
        ge=1,
    )

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level",
    )

    # Authentication
    jwt_secret_key: str = Field(
        default="your-secret-key-here-change-in-production",
        description="JWT secret key for token signing (CHANGE IN PRODUCTION!)",
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description="JWT signing algorithm",
    )
    jwt_expiration_minutes: int = Field(
        default=60,
        description="JWT token expiration time in minutes",
        ge=1,
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate and normalize log level."""
        if isinstance(v, str):
            v = v.upper()
        # Validation will happen after this
        return v

    model_config = SettingsConfigDict(
        env_prefix="PULSAR_",  # Environment variables: PULSAR_HTTP_PORT, etc.
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra fields in config file
        # This is important: env vars have priority over init values
        env_nested_delimiter="__",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Customize settings sources priority.

        Priority order (highest to lowest):
        1. Environment variables
        2. .env file
        3. Init settings (config file values)
        4. Default values
        """
        return (
            env_settings,  # Highest priority
            dotenv_settings,  # Second priority
            init_settings,  # Config file values (third priority)
            file_secret_settings,
        )

    @classmethod
    def from_config_file(cls, config_path: Optional[Path] = None) -> "Settings":
        """Create Settings instance from config file.

        Args:
            config_path: Optional path to config file

        Returns:
            Settings instance with values from config file and environment
        """
        # Load config file
        config_data = load_config_file(config_path)

        # Create settings with config file data
        # Environment variables will override due to settings_customise_sources
        return cls(**config_data)


def load_settings(config_path: Optional[str] = None) -> Settings:
    """Load application settings from config file and environment.

    Configuration precedence (highest to lowest):
    1. Environment variables (PULSAR_*)
    2. Config file (config.toml or config.yaml)
    3. Default values

    Args:
        config_path: Optional path to config file

    Returns:
        Settings instance with all configuration loaded
    """
    # Check for config file path from environment
    if config_path is None:
        config_path = os.getenv("PULSAR_CONFIG_FILE")

    # Convert to Path if string
    path_obj = Path(config_path) if config_path else None

    # Load settings
    settings = Settings.from_config_file(path_obj)

    # Log configuration sources
    logger.info("Configuration loaded successfully")
    logger.info(f"  Storage backend: {settings.storage_backend}")
    logger.info(f"  Log level: {settings.log_level}")

    if settings.jwt_secret_key == "your-secret-key-here-change-in-production":
        logger.warning(
            "⚠️  WARNING: Using default JWT secret key! " "Set PULSAR_JWT_SECRET_KEY environment variable in production!"
        )

    return settings


# Global settings instance
# This will be loaded when the module is imported
try:
    settings = load_settings()
except Exception as e:
    logger.warning(f"Error loading config file, using defaults: {e}")
    settings = Settings()
