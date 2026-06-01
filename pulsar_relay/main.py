"""Main FastAPI application."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from pulsar_relay.api import auth, device, health, messages, oidc, polling, topics, websocket
from pulsar_relay.auth.denylist import InMemoryJWTDenylist, JWTDenylistStorage, ValkeyJWTDenylist
from pulsar_relay.auth.dependencies import (
    set_device_code_storage,
    set_jwt_denylist,
    set_oidc_clients,
    set_oidc_state_storage,
    set_refresh_token_storage,
    set_topic_storage,
    set_user_storage,
)
from pulsar_relay.auth.device_flow import (
    DeviceCodeStorage,
    InMemoryDeviceCodeStorage,
    ValkeyDeviceCodeStorage,
)
from pulsar_relay.auth.jwt import hash_password, verify_password
from pulsar_relay.auth.models import UserCreate
from pulsar_relay.auth.oidc_client import OIDCClient
from pulsar_relay.auth.oidc_state import (
    InMemoryOIDCStateStorage,
    OIDCStateStorage,
    ValkeyOIDCStateStorage,
)
from pulsar_relay.auth.refresh import (
    InMemoryRefreshTokenStorage,
    RefreshTokenStorage,
    ValkeyRefreshTokenStorage,
)
from pulsar_relay.auth.storage import InMemoryUserStorage, UserStorage, ValkeyUserStorage
from pulsar_relay.auth.topic_storage import (
    InMemoryTopicStorage,
    TopicStorage,
    ValkeyTopicStorage,
    scan_for_legacy_keys,
)
from pulsar_relay.config import settings, validate_startup_secrets
from pulsar_relay.core.connections import ConnectionManager
from pulsar_relay.core.idempotency import (
    IdempotencyStorage,
    InMemoryIdempotencyStorage,
    ValkeyIdempotencyStorage,
)
from pulsar_relay.core.polling import PollManager
from pulsar_relay.core.pubsub import PubSubCoordinator
from pulsar_relay.storage.memory import MemoryStorage
from pulsar_relay.storage.valkey import ValkeyStorage

log = logging.getLogger(__name__)


def _init_sentry(config):
    """Initialize Sentry error reporting if configured.

    Returns the ``sentry_sdk`` module when reporting is active, otherwise
    ``None``. Reporting is active only when ``config.sentry_dsn`` is set AND
    the optional ``sentry`` extra is installed (pip install
    pulsar-relay[sentry]). Sentry's FastAPI/Starlette integration
    auto-instruments the app as long as ``init`` runs before the app is
    created, which is why this is called at import time below.
    """
    if not config.sentry_dsn:
        return None
    try:
        import sentry_sdk
    except ImportError:
        log.warning(
            "PULSAR_SENTRY_DSN is set but sentry-sdk is not installed; error "
            "reporting disabled. Install pulsar-relay[sentry] to enable it."
        )
        return None
    sentry_sdk.init(
        dsn=config.sentry_dsn,
        environment=config.sentry_environment,
        traces_sample_rate=config.sentry_traces_sample_rate,
        send_default_pii=config.sentry_send_default_pii,
    )
    log.info("Sentry error reporting enabled (environment=%s)", config.sentry_environment)
    return sentry_sdk


# Set up Sentry before the FastAPI app is created so its integration can
# instrument the app. ``None`` when unconfigured / sentry-sdk absent.
_sentry_sdk = _init_sentry(settings)

# Use uvloop for better performance (optional, requires Python <=3.12)
try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass  # uvloop not available, using default event loop


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan events (startup and shutdown)."""
    # Startup: Initialize all services
    log.info("Starting up application...")

    # Refuse to start when secrets are at defaults / unset. Raises
    # InsecureDefaultsError (a SystemExit subclass) so uvicorn propagates a
    # clean non-zero exit. Bypassed by PULSAR_ALLOW_INSECURE_DEFAULTS=1.
    validate_startup_secrets(settings)

    # Initialize storage based on configuration
    if settings.storage_backend == "valkey":
        log.info("Using Valkey storage backend")
        storage: Union[ValkeyStorage, MemoryStorage] = ValkeyStorage(
            host=settings.valkey_host,
            port=settings.valkey_port,
            max_messages_per_topic=settings.max_messages_per_topic,
            use_tls=settings.valkey_use_tls,
            username=settings.valkey_username,
            password=settings.valkey_password,
        )
        assert isinstance(storage, ValkeyStorage)
        # Connect to Valkey
        await storage.connect()
        log.info(f"Connected to Valkey at {settings.valkey_host}:{settings.valkey_port}")

        # Scan for pre-namespacing topic keys (API H#5 migration). The
        # Phase 3c security fix moves topic keys from a flat namespace
        # (``topic:{name}``) to ``topic:{owner_id}/{name}``. Mixing old
        # and new shapes silently breaks every storage code path; we
        # refuse to start when legacy keys are present. Bypassable
        # via PULSAR_ALLOW_INSECURE_DEFAULTS=1 for local-dev.
        legacy_keys = await scan_for_legacy_keys(storage._client, limit=20)
        if legacy_keys:
            msg = (
                "Refusing to start: found pre-Phase-3c flat-namespace topic keys in Valkey. "
                f"Examples: {legacy_keys[:5]}. Topics are now keyed by (owner_id, name). "
                "Migrate by FLUSHing the topic/stream/meta keys or by re-creating topics "
                "under each owner. To bypass for local-dev set PULSAR_ALLOW_INSECURE_DEFAULTS=1."
            )
            if settings.allow_insecure_defaults:
                log.warning(msg)
            else:
                log.error(msg)
                raise SystemExit(2)

        topic_storage: TopicStorage = ValkeyTopicStorage(storage._client)
        log.info("Initialized Valkey Topic Storage")
        user_storage: UserStorage = ValkeyUserStorage(storage._client)
        log.info("Initialized Valkey User Storage")
        refresh_storage: RefreshTokenStorage = ValkeyRefreshTokenStorage(storage._client)
        device_storage: DeviceCodeStorage = ValkeyDeviceCodeStorage(storage._client)
        oidc_state_storage: OIDCStateStorage = ValkeyOIDCStateStorage(storage._client)
        jwt_denylist: JWTDenylistStorage = ValkeyJWTDenylist(storage._client)
        idempotency_storage: IdempotencyStorage = ValkeyIdempotencyStorage(storage._client)
        log.info("Initialized Valkey refresh/device/oidc-state/jwt-denylist/idempotency storage")
    else:
        log.info("Using in-memory storage backend")
        storage = MemoryStorage(max_messages_per_topic=settings.max_messages_per_topic)
        # Initialize user storage
        user_storage = InMemoryUserStorage()
        log.info("Initialized In-Memory User Storage")
        topic_storage = InMemoryTopicStorage()
        log.info("Initialized In-Memory Topic Storage")
        refresh_storage = InMemoryRefreshTokenStorage()
        device_storage = InMemoryDeviceCodeStorage()
        oidc_state_storage = InMemoryOIDCStateStorage()
        jwt_denylist = InMemoryJWTDenylist()
        idempotency_storage = InMemoryIdempotencyStorage()
        log.info("Initialized in-memory refresh/device/oidc-state/jwt-denylist/idempotency storage")

    # Initialize connection manager
    connection_manager = ConnectionManager()
    log.info("Initialized Connection Manager %s", connection_manager)

    # Initialize poll manager
    poll_manager = PollManager()
    log.info("Initialized Poll Manager")

    # Initialize pub/sub coordinator for multi-worker message broadcasting
    # Only enable if using Valkey backend (required for cross-worker coordination)
    pubsub_coordinator = None
    if settings.storage_backend == "valkey" and isinstance(storage, ValkeyStorage):
        assert storage._client is not None, "Valkey client should be connected"
        pubsub_coordinator = PubSubCoordinator(storage._client)

        # Register handlers to broadcast messages to local clients
        async def handle_pubsub_message(topic: str, message_data: dict) -> None:
            """Handle incoming pub/sub messages and broadcast to local clients."""
            # Broadcast to WebSocket clients on this worker
            await connection_manager.broadcast(topic, message_data)

            # Broadcast to long-polling clients on this worker
            await poll_manager.broadcast_to_topic(topic, message_data)

        pubsub_coordinator.register_handler(handle_pubsub_message)
        await pubsub_coordinator.start()
        log.info("Initialized PubSub Coordinator for multi-worker broadcasting")

    set_user_storage(user_storage)
    set_topic_storage(topic_storage)
    set_refresh_token_storage(refresh_storage)
    set_device_code_storage(device_storage)
    set_oidc_state_storage(oidc_state_storage)
    set_jwt_denylist(jwt_denylist)

    # Build OIDC clients (one per configured provider). Empty when oidc.enabled=False.
    oidc_clients: dict[str, OIDCClient] = {}
    oidc_auth_urls: dict[str, str] = {}
    if settings.oidc.enabled:
        for name, provider_cfg in settings.oidc.providers.items():
            client_inst = OIDCClient(name, provider_cfg)
            oidc_clients[name] = client_inst
            # Resolve discovery so Swagger UI can reference the authorization
            # endpoint synchronously when generating the OpenAPI schema. The
            # OIDC client caches the discovery document.
            try:
                discovered = await client_inst._discover()
                oidc_auth_urls[name] = discovered.authorization_endpoint
            except Exception as exc:  # noqa: BLE001 — best-effort, log + skip
                log.warning(
                    "OIDC discovery failed for provider %s; Swagger UI will not "
                    "show the OIDC button until /docs is reloaded. (%s)",
                    name,
                    exc,
                )
        log.info("Initialized %d OIDC providers: %s", len(oidc_clients), list(oidc_clients))
    set_oidc_clients(oidc_clients)
    # Stash auth URLs on app.state so the openapi() override can pick them up.
    app.state.oidc_auth_urls = oidc_auth_urls
    # Reset any cached schema so the next /openapi.json reflects the new auth URLs.
    app.openapi_schema = None

    # Bootstrap admin user if configured
    if settings.bootstrap_admin_username and settings.bootstrap_admin_password:
        try:
            # Check if admin already exists
            existing_admin = await user_storage.get_user_by_username(settings.bootstrap_admin_username)
            if not existing_admin:
                admin_data = UserCreate(
                    username=settings.bootstrap_admin_username,
                    password=settings.bootstrap_admin_password,
                    email=settings.bootstrap_admin_email or f"{settings.bootstrap_admin_username}@example.com",
                    permissions=["admin", "read", "write"],
                )
                admin_user = await user_storage.create_user(admin_data)
                log.info(f"✅ Bootstrap admin created: {admin_user.username}")
            else:
                log.info(f"Bootstrap admin already exists: {settings.bootstrap_admin_username}")
                # The previous code compared ``hash_password(plaintext)``
                # against the stored hash for equality — but the
                # argon2 hash includes a fresh salt every call, so the
                # comparison was effectively always-False and the
                # bootstrap password would be re-hashed and re-stored
                # on every startup (API M#15). Use ``verify_password``
                # which compares plaintext to a stored argon2 hash
                # correctly, and only update when the env-supplied
                # password no longer matches what's stored.
                if existing_admin.hashed_password is None or not verify_password(
                    settings.bootstrap_admin_password, existing_admin.hashed_password
                ):
                    existing_admin.hashed_password = hash_password(settings.bootstrap_admin_password)
                    await user_storage.update_user(existing_admin)
                    log.info("Bootstrap admin password updated from env")
        except Exception as e:
            log.error(f"Failed to create bootstrap admin: {e}")

    # Store in app state for access in routes
    app.state.storage = storage
    app.state.poll_manager = poll_manager
    app.state.user_storage = user_storage
    app.state.topic_storage = topic_storage
    app.state.pubsub_coordinator = pubsub_coordinator
    app.state.idempotency_storage = idempotency_storage

    # Inject dependencies into API routers
    messages.set_storage(storage)
    messages.set_manager(connection_manager)
    messages.set_poll_manager(poll_manager)
    messages.set_pubsub_coordinator(pubsub_coordinator)
    health.set_storage(storage)
    topics.set_storage(storage)
    websocket.set_manager(connection_manager)

    # Periodic sweep of stale long-poll waiters. ``cleanup_stale_waiters``
    # used to be defined but never invoked, so an abandoned waiter sat
    # in memory forever (API H#8).
    cleanup_task = asyncio.create_task(poll_manager.cleanup_loop())

    log.info("Application startup complete")

    yield  # Application is running

    # Shutdown: Cleanup resources
    log.info("Shutting down application...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    if pubsub_coordinator:
        await pubsub_coordinator.stop()
    await storage.close()
    log.info("Application shutdown complete")


# Create FastAPI app with lifespan handler.
# The previous Swagger UI OIDC integration was driven by the now-removed
# ``/auth/oidc/{provider}/swagger-token`` bridge endpoint. To authenticate
# Swagger via OIDC, operators should open ``/auth/oidc/{provider}/login``
# in another tab and paste the resulting access token into Swagger's
# bearer authorization. The password flow is unchanged.
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="High-performance message relay with WebSocket and long-polling support",
    lifespan=lifespan,
)

# Wire up the slowapi rate limiter. The module-level Limiter instance
# lives in pulsar_relay.api.limits so route modules can import it
# without import cycles; here we attach it to the FastAPI app and
# register the 429 exception handler. We intentionally do NOT use
# ``SlowAPIMiddleware`` — it expects a ``Response`` object to inject
# rate-limit headers and breaks the request flow when used together
# with the per-route ``@limiter.limit`` decorator (which is what we
# rely on for enforcement).
from pulsar_relay.api.limits import limiter  # noqa: E402 — must follow ``app`` creation

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


class BodySizeLimitMiddleware:
    """Reject HTTP requests whose Content-Length exceeds ``max_bytes``.

    Implemented as a raw ASGI middleware so the rejection happens before
    the body is buffered by FastAPI / Pydantic — a 100 MiB payload never
    makes it into memory. WebSocket upgrades are passed through
    unchanged.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > self.max_bytes:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b'{"error":"PAYLOAD_TOO_LARGE","message":"request body exceeds configured limit"}',
                        }
                    )
                    return
                break
        await self.app(scope, receive, send)


# Order matters: TrustedHost runs first (cheapest reject), then CORS,
# then body-size. CORS runs *after* TrustedHost so a hostile Host header
# is rejected before CORS preflights advertise allowed origins. The
# body-size middleware runs *inside* CORS so CORS preflights for big
# requests still get the right CORS headers on the 413 — Starlette nests
# user_middleware in registration order, with the first .add_middleware
# call ending up outermost.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


# Include routers
app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["authentication"])
app.include_router(oidc.router)
app.include_router(device.router)
app.include_router(topics.router)
app.include_router(messages.router)
app.include_router(websocket.router)
app.include_router(polling.router, prefix="/messages", tags=["polling"])

# Initialize Prometheus auto-instrumentation + /metrics endpoint when the
# optional dependency is installed. Skipping it is the supported path for
# downstream installs whose pinned starlette is incompatible with
# prometheus-fastapi-instrumentator's transitive constraint — the
# ``prometheus_client``-based counters/histograms in
# ``pulsar_relay.utils.metrics`` still record normally; only the
# auto-exposed ``/metrics`` route disappears.
try:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
except ImportError:
    log.info(
        "prometheus_fastapi_instrumentator not installed; /metrics endpoint disabled. "
        "Install pulsar-relay[metrics] to enable it."
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler.

    Never leaks ``str(exc)`` to clients — internal hostnames, Pydantic
    field names, and library error strings have been observed in past
    incidents. Operators get the full traceback via the server logs.
    """
    log.exception("Unhandled exception serving %s %s", request.method, request.url.path)
    # Report to Sentry explicitly. The catch-all handler returns a response,
    # so depending on the Starlette/sentry-sdk versions the integration may
    # not auto-capture; capturing here is version-independent and Sentry
    # dedupes events, so there's no double-reporting risk.
    if _sentry_sdk is not None:
        _sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "An internal error occurred",
        },
    )
