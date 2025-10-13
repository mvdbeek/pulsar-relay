"""Application configuration using Pydantic Settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server Configuration
    app_name: str = "Pulsar Proxy"
    http_port: int = 8080
    workers: int = 4

    # Storage Backend Selection
    storage_backend: Literal["memory", "valkey"] = "memory"

    # Valkey Configuration
    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_use_tls: bool = False

    # Storage Configuration
    hot_tier_retention: int = 600  # 10 minutes in seconds
    persistent_tier_retention: int = 86400  # 24 hours in seconds
    max_messages_per_topic: int = 1000000

    # Limits
    max_connections_per_instance: int = 10000
    max_message_size: int = 1048576  # 1MB
    rate_limit_per_client: int = 1000  # messages per minute

    # Logging
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# Global settings instance
settings = Settings()
