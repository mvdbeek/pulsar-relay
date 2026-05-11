# Connecting Pulsar Relay to an OIDC Provider

Pulsar Relay can delegate sign-in to any OpenID Connect provider (Keycloak, Google, Okta, Auth0, Entra ID, …). On first sign-in the relay auto-provisions a local user from the ID-token claims. Daemons without a browser use the OAuth 2.0 Device Authorization Grant (RFC 8628).

## 1. Register a client at your IdP

Create a confidential OAuth 2.0 / OIDC client and configure:

- **Redirect URI:** `https://<your-relay>/auth/oidc/<provider>/callback`
  `<provider>` is the dictionary key you give the provider in relay config (e.g. `keycloak`, `google`).
- **Grants:** Authorization Code + PKCE, and **Device Authorization Grant** if you want headless login.
- **Scopes:** `openid email profile` (default).
- Note the **client ID**, **client secret**, and either the **discovery URL** (`.well-known/openid-configuration`) or the explicit `issuer` / `authorization_endpoint` / `token_endpoint` / `jwks_uri`.

## 2. Configure the relay

OIDC settings live under `oidc.*`. Environment variables use `__` as the nesting delimiter. The block below is the minimum required to enable a provider:

```bash
export PULSAR_JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Top-level OIDC
export PULSAR_OIDC__ENABLED=true
export PULSAR_OIDC__BASE_URL=https://relay.example.com   # https required (http only for localhost)

# One provider, keyed as "keycloak" — change to suit
export PULSAR_OIDC__PROVIDERS__KEYCLOAK__DISPLAY_NAME="Keycloak"
export PULSAR_OIDC__PROVIDERS__KEYCLOAK__CLIENT_ID=pulsar-relay
export PULSAR_OIDC__PROVIDERS__KEYCLOAK__CLIENT_SECRET="$KEYCLOAK_CLIENT_SECRET"
export PULSAR_OIDC__PROVIDERS__KEYCLOAK__DISCOVERY_URL=https://kc.example.com/realms/main/.well-known/openid-configuration
# Optional: which ID-token claim to use as the local username (default: "email")
export PULSAR_OIDC__PROVIDERS__KEYCLOAK__CLAIM_USERNAME=preferred_username
```

Equivalent `config.toml` (keep secrets in env vars, not the file):

```toml
[oidc]
enabled = true
base_url = "https://relay.example.com"
default_permissions = ["read", "write"]   # granted on first sign-in

[oidc.providers.keycloak]
display_name = "Keycloak"
client_id = "pulsar-relay"
discovery_url = "https://kc.example.com/realms/main/.well-known/openid-configuration"
claim_username = "preferred_username"
```

### Reference: per-provider fields

| Field | Default | Notes |
|---|---|---|
| `display_name` | — | Required. Shown to operators. |
| `client_id` / `client_secret` | — | Required. |
| `scopes` | `["openid","email","profile"]` | |
| `discovery_url` | — | If set, all endpoints are auto-discovered. |
| `issuer`, `authorization_endpoint`, `token_endpoint`, `jwks_uri` | — | Required only if `discovery_url` is not set. |
| `userinfo_endpoint` | — | Optional. Falls back to ID-token claims. |
| `claim_username` / `claim_email` / `claim_sub` | `email` / `email` / `sub` | Map IdP claims onto local user fields. |

### Top-level OIDC fields

| Field | Default | Notes |
|---|---|---|
| `enabled` | `false` | Master switch. |
| `base_url` | — | Required when enabled. Must be `https://…` (or `http://localhost` for dev). |
| `default_permissions` | `["read","write"]` | Subset of `admin`, `read`, `write`. |
| `state_ttl_seconds` | `600` | Lifetime of in-flight auth state (60–3600). |

## 3. Endpoints the relay exposes

- `GET /auth/oidc/providers` — list configured providers.
- `GET /auth/oidc/{provider}/login` — start browser sign-in (Authorization Code + PKCE).
- `GET /auth/oidc/{provider}/callback` — redirect URI; returns relay JWTs.
- `POST /auth/device/code` / `POST /auth/device/token` — RFC 8628 device flow.
- `GET /auth/device?user_code=XXXX-XXXX` — operator approval page.
- `POST /auth/token/refresh` / `POST /auth/token/revoke` — refresh-token rotation and revocation.

## 4. Verify it works

1. Start the relay and check the logs report OIDC enabled.
2. `curl https://relay.example.com/auth/oidc/providers` — your provider should appear.
3. Open `https://relay.example.com/auth/oidc/<provider>/login` in a browser, sign in at the IdP, and confirm you receive a token response.
4. Swagger UI at `/docs` has an "Authorize" button wired to the same flow.

For a fully working local example (Keycloak + relay via Docker Compose) see `local-dev/README.md`.
