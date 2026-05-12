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
from urllib.parse import urlparse

import tomli
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Sentinel default for ``jwt_secret_key``. The startup guard refuses to boot
#: when the live value equals this constant (unless ``allow_insecure_defaults``
#: is set). Exported so tests can reference it without hard-coding the literal.
_DEFAULT_JWT_SECRET = "your-secret-key-here-change-in-production"

#: Minimum acceptable length for ``jwt_secret_key``. 32 chars is short for true
#: cryptographic strength but matches the longest legacy fixture and prevents
#: trivially-weak picks like "changeme".
_MIN_JWT_SECRET_LEN = 32


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
            Path("/etc/pulsar-relay/config.toml"),
            Path("/etc/pulsar-relay/config.yaml"),
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


_OIDC_DEFAULT_PERMISSIONS: tuple[Literal["admin", "read", "write"], ...] = ("read", "write")


class OIDCProviderConfig(BaseModel):
    """One configured upstream OIDC provider (Google, Keycloak, GitHub, etc.).

    Either ``discovery_url`` OR all of ``issuer``, ``authorization_endpoint``,
    ``token_endpoint``, ``jwks_uri`` must be provided. ``userinfo_endpoint`` is
    optional (we'll fall back to ID-token claims).
    """

    display_name: str = Field(..., description="Human-friendly label shown to operators")
    client_id: str = Field(...)
    client_secret: str = Field(..., description="Set via env var; never check into source")
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])

    # Either supply a discovery URL...
    discovery_url: Optional[str] = Field(
        None, description="OpenID-Connect discovery URL (.well-known/openid-configuration)"
    )

    # ...or explicit endpoints.
    issuer: Optional[str] = None
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None

    # Claim mapping. Defaults match Google/Keycloak. ``preferred_username``
    # is also a sensible default for some IdPs.
    claim_username: str = Field(default="email", description="ID-token/userinfo claim used as the local username")
    claim_email: str = Field(default="email")
    claim_sub: str = Field(default="sub")

    @model_validator(mode="after")
    def _require_discovery_or_endpoints(self) -> "OIDCProviderConfig":
        if self.discovery_url:
            return self
        missing = [
            name
            for name in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                "OIDC provider must set either discovery_url, or all of "
                f"issuer/authorization_endpoint/token_endpoint/jwks_uri (missing: {', '.join(missing)})"
            )
        return self


class OIDCConfig(BaseModel):
    """Top-level OIDC configuration."""

    enabled: bool = Field(default=False)
    base_url: Optional[str] = Field(
        None,
        description="Public base URL of the relay (used to build redirect_uri). Required when enabled=true.",
    )
    default_permissions: list[Literal["admin", "read", "write"]] = Field(
        default_factory=lambda: list(_OIDC_DEFAULT_PERMISSIONS),
        description="Permissions granted to auto-provisioned users on first sign-in",
    )
    providers: dict[str, OIDCProviderConfig] = Field(default_factory=dict)
    state_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    @model_validator(mode="after")
    def _check_base_url(self) -> "OIDCConfig":
        if not self.enabled:
            return self
        if not self.base_url:
            raise ValueError("oidc.base_url is required when oidc.enabled=true")
        parsed = urlparse(self.base_url)
        if parsed.scheme == "https":
            return self
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
            # Dev convenience only.
            logger.warning("OIDC base_url is http://localhost — only acceptable for development")
            return self
        raise ValueError("oidc.base_url must be https:// (http:// only allowed for localhost dev)")


class Settings(BaseSettings):
    """Application settings with support for config files and environment variables.

    Configuration loading order (highest to lowest priority):
    1. Environment variables (e.g. PULSAR_STORAGE_BACKEND=valkey)
    2. Config file (config.toml or config.yaml)
    3. Default values
    """

    # Server Configuration
    app_name: str = Field(
        default="Pulsar Relay",
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
    valkey_use_tls: bool = Field(
        default=False,
        description="Use TLS for Valkey connection",
    )
    valkey_username: Optional[str] = Field(
        default=None,
        description="Valkey ACL username (None = use legacy requirepass auth)",
    )
    valkey_password: Optional[str] = Field(
        default=None,
        description="Valkey password / ACL password. Required in production "
        "(startup refuses to boot without it unless allow_insecure_defaults).",
    )
    valkey_ca_path: Optional[str] = Field(
        default=None,
        description="Path to a CA bundle for TLS to Valkey. Only consulted when " "valkey_use_tls is True.",
    )

    # Storage Configuration
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

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level",
    )

    # Authentication
    jwt_secret_key: str = Field(
        default=_DEFAULT_JWT_SECRET,
        description="JWT secret key for token signing (CHANGE IN PRODUCTION!)",
    )

    # Startup safety
    allow_insecure_defaults: bool = Field(
        default=False,
        description="Bypass the startup guard that refuses to boot on default/"
        "missing secrets. Intended for local-dev compose and the test suite "
        "only; never set in production.",
    )

    # HTTP transport safety (CORS, host headers, WebSocket caps).
    # Empty defaults are refused by the startup guard unless
    # ``allow_insecure_defaults`` is set, so a misconfigured prod deploy
    # fails closed rather than silently accepting cross-origin traffic.
    allowed_origins: list[str] = Field(
        default_factory=list,
        description="Allow-list of HTTP Origin values for CORS and "
        "WebSocket Origin enforcement. Set explicitly; never use '*'.",
    )
    trusted_hosts: list[str] = Field(
        default_factory=list,
        description="Allow-list of Host header values accepted by "
        "TrustedHostMiddleware. Required to defend against Host-header "
        "spoofing when behind a reverse proxy.",
    )
    ws_max_per_user: int = Field(
        default=10,
        ge=1,
        description="Maximum concurrent /ws connections per authenticated user.",
    )
    ws_idle_seconds: int = Field(
        default=60,
        ge=5,
        description="Disconnect a WebSocket client that sends no frame within "
        "this many seconds. Defends against slow-loris connections.",
    )
    max_body_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        description="Reject HTTP requests with Content-Length above this "
        "value (1 MiB by default). Applied by the body-size middleware.",
    )

    # Refresh tokens & device flow
    refresh_token_ttl_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Refresh-token absolute lifetime in days",
    )
    device_code_ttl_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Device-authorization-grant code lifetime in seconds",
    )
    device_code_poll_interval: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Minimum poll interval (seconds) returned to device-flow daemons",
    )

    # OpenID Connect federation
    oidc: OIDCConfig = Field(default_factory=OIDCConfig)

    # Bootstrap Admin (created automatically on first startup if doesn't exist)
    bootstrap_admin_username: Optional[str] = Field(
        default=None,
        description="Bootstrap admin username (created on startup if set)",
    )
    bootstrap_admin_password: Optional[str] = Field(
        default=None,
        description="Bootstrap admin password (created on startup if set)",
    )
    bootstrap_admin_email: Optional[str] = Field(
        default=None,
        description="Bootstrap admin email (created on startup if set)",
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

    return settings


class InsecureDefaultsError(SystemExit):
    """Raised at startup when settings contain insecure defaults.

    Subclasses :class:`SystemExit` so an unhandled raise propagates as a
    process exit with the same exit code, but tests can still
    ``pytest.raises(InsecureDefaultsError)`` for fine-grained assertions.
    """

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message

    def __str__(self) -> str:
        return self.message


def validate_startup_secrets(settings: Settings) -> None:
    """Refuse to start when any required secret is missing or at a default.

    Checks (each raises :class:`InsecureDefaultsError` with a precise reason):

    * ``jwt_secret_key`` is the shipped default sentinel
    * ``jwt_secret_key`` is shorter than ``_MIN_JWT_SECRET_LEN`` characters
    * ``bootstrap_admin_password`` is unset
    * ``valkey_password`` is unset while the Valkey backend is selected

    Bypassed entirely when ``settings.allow_insecure_defaults`` is True —
    used by tests and the local-dev compose. Production deployments must
    leave the flag at its default (False).
    """
    if settings.allow_insecure_defaults:
        logger.warning(
            "PULSAR_ALLOW_INSECURE_DEFAULTS=1 — startup secret guard bypassed. "
            "This is unsafe outside local-dev / tests."
        )
        return

    if settings.jwt_secret_key == _DEFAULT_JWT_SECRET:
        raise InsecureDefaultsError(
            "Refusing to start: PULSAR_JWT_SECRET_KEY is the shipped default value. "
            "Generate a strong secret (e.g. `python -c 'import secrets; "
            "print(secrets.token_urlsafe(32))'`) and set PULSAR_JWT_SECRET_KEY. "
            "To bypass for local-dev/tests set PULSAR_ALLOW_INSECURE_DEFAULTS=1."
        )
    if len(settings.jwt_secret_key) < _MIN_JWT_SECRET_LEN:
        raise InsecureDefaultsError(
            f"Refusing to start: PULSAR_JWT_SECRET_KEY is shorter than " f"{_MIN_JWT_SECRET_LEN} characters."
        )
    if not settings.bootstrap_admin_password:
        raise InsecureDefaultsError(
            "Refusing to start: PULSAR_BOOTSTRAP_ADMIN_PASSWORD is not set. "
            "The bootstrap admin is required to administer the relay."
        )
    if settings.storage_backend == "valkey" and not settings.valkey_password:
        raise InsecureDefaultsError(
            "Refusing to start: PULSAR_VALKEY_PASSWORD is not set while "
            "PULSAR_STORAGE_BACKEND=valkey. The Valkey instance must require "
            "authentication."
        )
    if not settings.allowed_origins:
        raise InsecureDefaultsError(
            "Refusing to start: PULSAR_ALLOWED_ORIGINS is empty. Set the "
            "CORS / WebSocket Origin allow-list to the exact list of origins "
            "browsers will use (e.g. PULSAR_ALLOWED_ORIGINS='[\"https://relay.example.com\"]')."
        )
    if not settings.trusted_hosts:
        raise InsecureDefaultsError(
            "Refusing to start: PULSAR_TRUSTED_HOSTS is empty. Set the "
            "list of Host header values your reverse proxy forwards "
            "(e.g. PULSAR_TRUSTED_HOSTS='[\"relay.example.com\"]')."
        )


# Global settings instance
# This will be loaded when the module is imported
try:
    settings = load_settings()
except Exception as e:
    logger.warning(f"Error loading config file, using defaults: {e}")
    settings = Settings()
