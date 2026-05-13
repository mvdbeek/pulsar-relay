# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `pulsar-relay` server (`pyproject.toml`) and the `pulsar-relay-client` SDK (`client/pyproject.toml`) are versioned in lockstep and released together. Each version section below has `### Server` and `### Client` subsections; release workflows extract this version's block verbatim and use it as the GitHub Release body.

## [Unreleased]

## [0.2.1] - 2026-05-13

### Client

#### Fixed
- `normalize_relay_url` second clause was dead code — `allow_insecure_localhost` had no observable effect because the surrounding guard already restricted to non-localhost hosts. The flag now actually gates the localhost case as documented (defaulting to allow, so existing callers are unaffected).

#### Added
- `PULSAR_RELAY_ALLOW_INSECURE=1` environment variable disables the plaintext-to-non-localhost rejection in `normalize_relay_url`. Intended for test harnesses (e.g. fault injection through a non-TLS proxy) where the operator has explicitly opted in to insecure transport; never enable in production. Only the exact value `1` is honoured — truthy-looking values like `true` / `yes` fail closed.

## [0.2.0] - 2026-05-13

First coordinated release of the server and the sibling client distribution. The bulk of the work is the security review fallout shipped in PR #5 (security-hardening). The server is **not backwards-compatible** with `0.1.0` consumers — see *Breaking* below — and the client gets its first PyPI publish.

### Server

#### Added
- OIDC sign-in, RFC 8628 device authorization grant, and rotating refresh-token chains (landed pre-hardening on the `oidc` branch).
- Startup-secrets guard that refuses to boot on insecure defaults. Requires `PULSAR_JWT_SECRET_KEY` (≥ 32 chars, not the placeholder), `PULSAR_BOOTSTRAP_ADMIN_PASSWORD`, `PULSAR_VALKEY_PASSWORD` (when the Valkey backend is selected), `PULSAR_ALLOWED_ORIGINS`, and `PULSAR_TRUSTED_HOSTS`. `PULSAR_ALLOW_INSECURE_DEFAULTS=1` is the local-dev / CI escape hatch.
- `CORSMiddleware` + `TrustedHostMiddleware` driven by the two new env vars, plus a 1 MiB request-body size cap.
- JWT denylist: every access token now carries a `jti`; `/auth/logout` revokes the current `jti` in Valkey for its remaining TTL. In-memory and Valkey-backed denylist implementations.
- Refresh-token rotation uses a Lua-script CAS so concurrent refreshes cannot fork the chain.
- slowapi rate limiting on `/auth/login`, `/auth/token/refresh`, the device-flow endpoints, message publish, and bulk publish.
- `Idempotency-Key` end-to-end dedupe on message POSTs (`SET NX EX 600`).
- Long-poll cleanup task in the lifespan; bounded `asyncio.Queue` per waiter; per-user concurrent-waiter cap.
- Valkey `valkey.conf` hardened: `bind 127.0.0.1`, `protected-mode yes`, `FLUSHDB` / `FLUSHALL` / `CONFIG` / `DEBUG` / `SHUTDOWN` / `KEYS` renamed to empty strings.
- `Dockerfile` drops to a non-root `relay` user; `.env.example` no longer ships secret-shaped defaults.
- Federation collision guard: refuses to auto-provision when the username already belongs to a non-federated account; requires `email_verified=True` for email-as-username.
- WebSocket auth via `Sec-WebSocket-Protocol: bearer, bearer.<jwt>` (K8s-style sentinel + carrier). Per-user concurrent-WS cap, idle timeout, and Origin check.
- `Galaxy-BYOC` end-to-end tests against a live Keycloak harness (CI service container).

#### Changed
- **Topic identity is per-user.** Storage keys are `stream:topic:{owner_id}/{name}` and `meta:topic:{owner_id}/{name}`; two users picking the same bare name no longer collide.
- Access-token default TTL dropped (paired with the denylist).
- Bootstrap admin re-hash now uses `verify_password` instead of bcrypt-hash equality.
- HSET + EXPIRE pairs in device-flow / refresh / OIDC-state storage made atomic via GLIDE `Batch(is_atomic=True)`.

#### Removed
- `/auth/oidc/{provider}/swagger-token` (the cross-site code-exchange bridge). Use the documented OIDC code flow.
- `is_public` / `allowed_user_ids` / `TopicPermission` types — namespacing makes cross-user wire access impossible by construction, so the sharing scaffolding became dead code.
- `ValkeyStorage.clear()` (moved to a test-only subclass; `FLUSHALL` is renamed away in the hardened `valkey.conf`).
- Unused `ttl_seconds` storage parameter that never enforced anything.

#### Fixed
- `PubSubCoordinator` subscriber `GlideClient` now inherits credentials from the publisher; previously connected unauthenticated and crashed against AUTH-required Valkey.
- `pulsar_relay/api/topics.py` route order: `/stats` resolves before `/{topic_name}` so non-admin callers get 403, not a topic lookup.
- Device-flow HTML output is `html.escape`d (XSS hardening on user_code / client_hint / display_name).
- `ValkeyStorage.connect()` pins the AUTH username to `"default"` under legacy `--requirepass`; without it, Valkey 9 rejects empty-username `HELLO AUTH` with WRONGPASS while `redis-cli -a` works.

#### Security
- Closes all six Criticals (C1–C6) and the highest-priority Highs from the four-area review. See `1b84235` (merge of PR #5) for the audit trail.

#### Breaking
- `StorageBackend.save_message` / `get_messages` / `trim_topic` / `get_topic_length` now take `owner_id` as the first positional argument.
- `TopicStorage` methods all take `owner_id`. `is_public` / `allowed_user_ids` / `TopicPermission` types removed.
- WebSocket clients must send `Sec-WebSocket-Protocol: bearer, bearer.<jwt>`. Query-string tokens are rejected.
- Five new required env vars at boot (see *Added*).
- Startup refuses to boot if pre-namespacing Valkey keys are present; set `PULSAR_ALLOW_INSECURE_DEFAULTS=1` to bypass.

### Client (`pulsar-relay-client`)

First publish to PyPI.

#### Added
- HTTP client for the relay's topic-management and token endpoints (`pulsar_relay_client.topics`).
- `RelayTransport` with bounded retry (`max_attempts=6` default), exponential backoff, and `Idempotency-Key` per logical post — same key is reused across retries.
- `pulsar_relay_client.auth.build_auth_manager` strategy + refresh-token chain support.
- Long-poll cursor persistence: `tempfile.mkstemp` + `0o600` + parent-dir fsync.
- Refresh-token persistence via `CredentialsFile` (`O_NOFOLLOW` + parent-dir perm check + `fchmod` before write).
- `normalize_relay_url` rejects userinfo and non-localhost `http://` schemes; `urllib.parse.quote(safe="")` on every URL path segment.
- `pulsar_relay_client.testing.FakeAuthManager` for unit tests; the module emits `RuntimeWarning` at import to flag accidental production use.

## [0.1.0] - 2025-10-28

Initial alpha tag (`v0.1.0`). The client distribution did not exist yet; only the server was tagged.
