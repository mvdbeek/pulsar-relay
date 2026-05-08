# End-to-end OIDC tests

Boots Keycloak (via `docker compose`) and a `pulsar-relay` subprocess wired
up to it, then drives the OIDC sign-in / device-flow paths against the real
stack.

## Run

Requires Docker daemon access.

```sh
pytest -m e2e tests/e2e
```

The default `pytest` invocation skips this suite (`addopts = "-m 'not e2e'"`).

The Keycloak compose service is started in the session-scoped `keycloak`
fixture and torn down at the end. Each test that needs a relay subprocess
gets a fresh one via `relay_against_keycloak`, with the realm provisioned
through Keycloak's admin REST API at fixture setup time.

## What is covered

- `test_oidc_browser_signin_provisions_user_and_returns_tokens` — full
  authorization-code flow, asserts the user is auto-provisioned with the
  configured default permissions.
- `test_oidc_callback_is_idempotent` — re-signing in returns the same
  `user_id`.
- `test_device_flow_end_to_end` — the daemon polls `/auth/device/token`
  while a parallel "operator" completes the Keycloak sign-in via the
  device-flow bridge URL.
- `test_refresh_token_rotation_against_real_relay` — `/auth/login` issues a
  refresh token; `/auth/token/refresh` rotates it; replaying the original
  returns 401.

## Troubleshooting

If Keycloak fails to start, the fixture dumps its logs before failing the
test. The most common cause is a port conflict — set
`KEYCLOAK_HOST_PORT` to override the default `8089`.
