# pulsar-relay-client

Python client for the [pulsar-relay](https://github.com/mvdbeek/pulsar-relay) HTTP wire contract.

Used by:

- the Pulsar daemon's relay subscriber/auth layer
- Galaxy's BYOC (Bring-Your-Own-Compute) Pulsar manager
- pulsar-relay's own end-to-end tests

The package is the single source of truth for the relay wire contract; the
server (`pulsar-relay`) and the client (`pulsar-relay-client`) version
independently. A new client version that knows about a new wire feature
bumps the minor version; breaking changes bump major.

## Modules

| Module | Public surface |
|---|---|
| `pulsar_relay_client.auth` | `RelayAuthManager`, `PasswordAuthenticator`, `RefreshTokenAuthenticator`, `RelayAuthError` |
| `pulsar_relay_client.credentials` | `CredentialsFile`, `InMemoryCredentialsStore`, `utcnow_iso` |
| `pulsar_relay_client.device_flow` | `RelayDeviceFlowAuthenticator`, `DeviceFlowError` (RFC 8628 + the `pair=true` extension) |
| `pulsar_relay_client.transport` | `RelayTransport`, `RelayTransportError` (HTTP + auth + cursor persistence) |
| `pulsar_relay_client.topics` | `HttpRelayClient` with `create_or_verify_topic`, the `RelayClient` Protocol (caller-facing typing surface), and the relay-error hierarchy. Topic naming conventions are the caller's concern. |
| `pulsar_relay_client.testing` | `FakeRelayClient` — in-memory implementation of the `RelayClient` Protocol for consumers writing tests against a fake. |

## Installation

```bash
pip install pulsar-relay-client
```

## Quick start

```python
from pulsar_relay_client import RelayTransport

transport = RelayTransport(
    "https://relay.example.org",
    credentials_file="/etc/pulsar/relay_credentials.json",
    cursor_path="/var/lib/pulsar/relay_cursor.json",
)
transport.post_message("job_status_update_my_manager", {"job_id": "j1", "state": "ok"})
messages = transport.long_poll(["job_setup_my_manager"], timeout=30)
```

## Versioning

- `1.0.0` — first standalone release. Wire features: device-flow with `pair=true`,
  chain-scoped revocation, topic create-or-verify-ownership, refresh-token rotation
  with `InMemoryCredentialsStore` callback.
