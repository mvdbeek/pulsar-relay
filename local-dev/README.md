# Local OIDC dev stack

Brings up Keycloak + Valkey + a live-reloading pulsar-relay so you can
exercise the OIDC sign-in and device-flow endpoints by hand, with hot
reload as you edit code.

## What you get

- **Keycloak 26** at <http://localhost:8080>
  - admin console: user `admin`, password `adminpassword`
  - realm `pulsar-local` (auto-imported on first boot)
  - users:
    - `alice` / `alicepass` — `alice@example.com`
    - `bob` / `bobpass` — `bob@example.com`
  - client `pulsar-relay` with secret `pulsar-local-secret`,
    standard flow + device flow + PKCE all enabled.
- **Valkey 8** at `localhost:6379` (Redis-compatible).
- **pulsar-relay** at <http://localhost:9000>, mounted from the host so
  saving a file under `pulsar_relay/` reloads the server automatically.
  - bootstrap admin: `admin` / `adminpw1234`

## Spin it up

```sh
cd local-dev
docker compose up -d
docker compose logs -f relay   # optional: watch the relay
```

First boot of Keycloak takes ~30 seconds while it imports the realm.

## Try the browser flow

1. Open <http://localhost:9000/auth/oidc/providers> to see the configured
   provider list.
2. Open <http://localhost:9000/auth/oidc/keycloak/login> in a browser.
   Keycloak will prompt for a username/password.
3. Sign in as `alice` / `alicepass`. You'll be redirected to the relay's
   callback, which returns a JSON document containing an access token.
4. Use the token against any protected endpoint, e.g.:
   ```sh
   ACCESS=...   # the access_token from step 3
   curl -s http://localhost:9000/auth/me -H "Authorization: Bearer $ACCESS" | jq
   ```

## Try the OIDC flow inside `/docs`

> **Note**: Swagger UI's "Logout" button only clears its own access-token
> cache; it doesn't sign out of the IdP. To switch users, clear the
> Keycloak cookies for `localhost:8080` (DevTools → Application → Cookies)
> and click Authorize again. This is a Swagger UI limitation, not a
> relay-side bug.


Open <http://localhost:9000/docs> and click **Authorize**. You'll see two
schemes:

- `OAuth2PasswordBearer` — the legacy username/password flow. Use the
  bootstrap admin (`admin` / `adminpw1234`).
- `OIDC_keycloak` — full authorization-code + PKCE flow against
  Keycloak. Pick this, click "Authorize", sign in as `alice` /
  `alicepass`, and Swagger UI will end up holding a *relay-issued* JWT
  (not a Keycloak token). The bridge endpoint
  `POST /auth/oidc/keycloak/swagger-token` does the exchange.

Once authorized, every "Try it out" call in the docs page is sent with
the relay JWT.

## Try the device flow (no host browser dependency on the daemon)

Simulating what `pulsar-config --login` does:

```sh
# 1. Daemon side: request a device code.
curl -s -X POST http://localhost:9000/auth/device/code \
  -d "client_hint=manual on $(hostname)" | jq

# Copy the user_code + verification_uri_complete from the response.
# 2. Operator side: open the verification URL in any browser, pick the
#    Keycloak button, sign in as alice/alicepass.
# 3. Daemon side: poll the token endpoint until success.
DEVICE_CODE=...   # from step 1
curl -s -X POST http://localhost:9000/auth/device/token \
  -d "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
  -d "device_code=$DEVICE_CODE" | jq
```

## Scripted end-to-end smoke test

The bundled `./smoke.sh` exercises the full device flow end-to-end
(driving Keycloak's HTML login form via Python), then rotates the
refresh token and proves that replaying the rotated token returns 401:

```sh
./smoke.sh
```

It expects `curl`, `jq`, and `python3` with the `httpx` package on the
host (the same package the project already pulls in for tests).

## Iterating on the relay

The `pulsar_relay` source directory is bind-mounted into the container
read-only and `uvicorn --reload` is wired up. Save a file in
`pulsar_relay/...` and the relay restarts within a second or two.

If you change a dependency in `pyproject.toml` you need to rebuild:

```sh
docker compose build relay
docker compose up -d relay
```

## Tear down

```sh
docker compose down -v
```

`-v` clears Valkey + Keycloak's H2 store, which is useful when iterating
on the realm import.

## Notes on the Keycloak hostname dance

Keycloak's discovery doc has to return URLs that work for two different
callers:

- the **browser** (running on the host) reaches Keycloak via
  `http://localhost:8080`,
- the **relay container** reaches it via the docker network alias
  `http://keycloak:8080`.

`KC_HOSTNAME=http://localhost:8080` pins the *frontchannel* URLs (the
authorization endpoint that the browser is redirected to, plus the `iss`
claim in tokens). `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true` lets the
*backchannel* URLs (token, jwks, userinfo) reflect whatever Host header
the request came in on, so the relay's discovery fetch returns
`keycloak:8080` URLs that it can actually reach.

If you hit "issuer mismatch" or "JWKS fetch failed" errors after editing
the compose file, double-check those two settings.
