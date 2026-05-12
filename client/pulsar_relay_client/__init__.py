"""pulsar-relay-client — Python client library for the pulsar-relay HTTP wire contract."""

from ._url import RelayURLError
from .auth import (
    PasswordAuthenticator,
    RefreshTokenAuthenticator,
    RelayAuthError,
    RelayAuthManager,
    build_auth_manager,
)
from .credentials import (
    CredentialsFile,
    CredentialsStore,
    InMemoryCredentialsStore,
    utcnow_iso,
)
from .device_flow import (
    DeviceFlowError,
    RelayDeviceFlowAuthenticator,
)
from .topics import (
    HttpRelayClient,
    RefreshTokenRejectedError,
    RelayClient,
    RelayClientError,
    RelayClientFactory,
    TopicOwnershipConflictError,
    default_relay_client_factory,
)
from .transport import (
    RelayTransport,
    RelayTransportError,
)

__version__ = "1.0.0"

__all__ = [
    # url validation
    "RelayURLError",
    # auth
    "PasswordAuthenticator",
    "RefreshTokenAuthenticator",
    "RelayAuthError",
    "RelayAuthManager",
    "build_auth_manager",
    # credentials
    "CredentialsFile",
    "CredentialsStore",
    "InMemoryCredentialsStore",
    "utcnow_iso",
    # device flow
    "DeviceFlowError",
    "RelayDeviceFlowAuthenticator",
    # transport
    "RelayTransport",
    "RelayTransportError",
    # topics
    "HttpRelayClient",
    "RelayClient",
    "RelayClientError",
    "RelayClientFactory",
    "RefreshTokenRejectedError",
    "TopicOwnershipConflictError",
    "default_relay_client_factory",
    # version
    "__version__",
]
