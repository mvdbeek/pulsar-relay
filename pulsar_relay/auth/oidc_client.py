"""OpenID Connect Relying-Party plumbing.

Per-provider client that knows how to:
- resolve discovery (cached),
- build a PKCE authorization URL,
- exchange an authorization code for tokens,
- validate the resulting ID token against the provider's JWKS.

Backed by ``httpx`` for HTTP and ``joserfc`` for JWT/JWS/JWK.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from joserfc import jwt as joserfc_jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from pulsar_relay.config import OIDCProviderConfig

logger = logging.getLogger(__name__)


# Asymmetric algorithms acceptable for ID-token signatures. We deliberately
# reject HS* (would imply we share the IdP's signing secret) and "none".
_ALLOWED_ID_TOKEN_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA")

# Cap on JWKS cache lifetime regardless of Cache-Control hints.
_JWKS_MAX_TTL_SECONDS = 24 * 60 * 60
_JWKS_DEFAULT_TTL_SECONDS = 600


@dataclass
class _Discovered:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    fetched_at: float


@dataclass
class _CachedJWKS:
    keyset: KeySet
    expires_at: float


@dataclass
class TokenSet:
    """Result of an authorization-code exchange."""

    access_token: str
    id_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None
    scope: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class OIDCError(Exception):
    """Raised when an OIDC operation fails (network, validation, claims)."""


def code_challenge_for(verifier: str) -> str:
    """RFC 7636 S256 code_challenge derived from a verifier."""
    digest = sha256(verifier.encode("ascii")).digest()
    # Base64url without padding
    import base64

    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _max_age_from_cache_control(header: str | None) -> int | None:
    if not header:
        return None
    match = re.search(r"max-age\s*=\s*(\d+)", header, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class OIDCClient:
    """Per-provider RP client. Holds discovery + JWKS caches."""

    def __init__(
        self,
        provider_name: str,
        config: OIDCProviderConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock_skew_seconds: int = 60,
    ) -> None:
        self.provider_name = provider_name
        self.config = config
        self._http = http_client  # if None, we create per-call clients
        self._clock_skew = clock_skew_seconds
        self._discovered: _Discovered | None = None
        self._jwks: _CachedJWKS | None = None

    # ---- HTTP helpers -------------------------------------------------------

    async def _get(self, url: str) -> httpx.Response:
        if self._http is not None:
            return await self._http.get(url)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.get(url)

    async def _post(self, url: str, *, data: dict[str, str], auth: tuple[str, str] | None = None) -> httpx.Response:
        # httpx rejects ``auth=None`` (it expects a sentinel). Build a kwargs
        # dict so we omit the key entirely when no auth is configured.
        kwargs: dict[str, Any] = {"data": data}
        if auth is not None:
            kwargs["auth"] = auth
        if self._http is not None:
            return await self._http.post(url, **kwargs)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(url, **kwargs)

    # ---- discovery ---------------------------------------------------------

    async def _discover(self) -> _Discovered:
        if self._discovered is not None:
            return self._discovered

        cfg = self.config
        if cfg.discovery_url:
            resp = await self._get(cfg.discovery_url)
            if resp.status_code != 200:
                raise OIDCError(f"OIDC discovery failed for {self.provider_name}: HTTP {resp.status_code}")
            doc = resp.json()
            self._discovered = _Discovered(
                issuer=doc["issuer"],
                authorization_endpoint=doc["authorization_endpoint"],
                token_endpoint=doc["token_endpoint"],
                jwks_uri=doc["jwks_uri"],
                userinfo_endpoint=doc.get("userinfo_endpoint"),
                fetched_at=time.time(),
            )
        else:
            assert cfg.issuer and cfg.authorization_endpoint and cfg.token_endpoint and cfg.jwks_uri
            self._discovered = _Discovered(
                issuer=cfg.issuer,
                authorization_endpoint=cfg.authorization_endpoint,
                token_endpoint=cfg.token_endpoint,
                jwks_uri=cfg.jwks_uri,
                userinfo_endpoint=cfg.userinfo_endpoint,
                fetched_at=time.time(),
            )
        return self._discovered

    # ---- JWKS --------------------------------------------------------------

    async def _get_jwks(self) -> KeySet:
        now = time.time()
        if self._jwks is not None and self._jwks.expires_at > now:
            return self._jwks.keyset

        discovered = await self._discover()
        resp = await self._get(discovered.jwks_uri)
        if resp.status_code != 200:
            raise OIDCError(f"JWKS fetch failed for {self.provider_name}: HTTP {resp.status_code}")
        keyset = KeySet.import_key_set(resp.json())

        max_age = _max_age_from_cache_control(resp.headers.get("Cache-Control"))
        ttl = min(max(max_age or _JWKS_DEFAULT_TTL_SECONDS, 60), _JWKS_MAX_TTL_SECONDS)
        self._jwks = _CachedJWKS(keyset=keyset, expires_at=now + ttl)
        return keyset

    # ---- authorization request --------------------------------------------

    async def build_authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_verifier: str,
        extra_params: dict[str, str] | None = None,
    ) -> str:
        discovered = await self._discover()
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge_for(code_verifier),
            "code_challenge_method": "S256",
        }
        if extra_params:
            params.update(extra_params)
        sep = "&" if "?" in discovered.authorization_endpoint else "?"
        return f"{discovered.authorization_endpoint}{sep}{urlencode(params)}"

    # ---- code exchange + ID token validation ------------------------------

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> TokenSet:
        discovered = await self._discover()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.config.client_id,
        }
        # client_secret is sent via Basic auth for confidential clients per OAuth2 best practice.
        auth = (self.config.client_id, self.config.client_secret)
        resp = await self._post(discovered.token_endpoint, data=data, auth=auth)
        if resp.status_code != 200:
            raise OIDCError(f"Token exchange failed for {self.provider_name}: HTTP {resp.status_code} {resp.text!r}")
        body = resp.json()
        if "id_token" not in body:
            raise OIDCError(f"Token response missing id_token for {self.provider_name}")
        return TokenSet(
            access_token=body.get("access_token", ""),
            id_token=body["id_token"],
            refresh_token=body.get("refresh_token"),
            token_type=body.get("token_type", "Bearer"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw=body,
        )

    async def validate_id_token(
        self,
        id_token: str,
        *,
        nonce: str | None,
    ) -> dict[str, Any]:
        """Validate signature + standard claims and return the parsed claims dict."""
        discovered = await self._discover()
        keyset = await self._get_jwks()

        try:
            decoded = joserfc_jwt.decode(id_token, keyset, algorithms=list(_ALLOWED_ID_TOKEN_ALGS))
        except JoseError as exc:
            # On signature failure, retry once with a refreshed JWKS in case
            # the IdP rotated keys mid-session.
            self._jwks = None
            keyset = await self._get_jwks()
            try:
                decoded = joserfc_jwt.decode(id_token, keyset, algorithms=list(_ALLOWED_ID_TOKEN_ALGS))
            except JoseError as exc2:
                raise OIDCError(f"ID token signature validation failed: {exc2}") from exc

        claims = dict(decoded.claims)

        # Claim validation. We do this manually rather than via JWTClaimsRegistry
        # so we can give specific error messages.
        now = int(time.time())
        skew = self._clock_skew

        if claims.get("iss") != discovered.issuer:
            raise OIDCError(f"ID token issuer mismatch (expected {discovered.issuer}, got {claims.get('iss')})")
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        if self.config.client_id not in aud_list:
            raise OIDCError(f"ID token audience does not include client_id {self.config.client_id}")

        exp = claims.get("exp")
        if exp is None or int(exp) + skew < now:
            raise OIDCError("ID token expired")
        nbf = claims.get("nbf")
        if nbf is not None and int(nbf) - skew > now:
            raise OIDCError("ID token not yet valid")
        iat = claims.get("iat")
        if iat is not None and int(iat) - skew > now:
            raise OIDCError("ID token issued in the future")

        if nonce is not None and claims.get("nonce") != nonce:
            raise OIDCError("ID token nonce mismatch")

        if not claims.get("sub"):
            raise OIDCError("ID token missing 'sub' claim")

        return claims

    async def fetch_userinfo(self, access_token: str) -> dict[str, Any] | None:
        """Optional: fetch userinfo to fill in claims absent from the ID token."""
        discovered = await self._discover()
        if not discovered.userinfo_endpoint:
            return None
        if self._http is not None:
            client = self._http
            close = False
        else:
            client = httpx.AsyncClient(timeout=10.0)
            close = True
        try:
            resp = await client.get(
                discovered.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                return None
            return cast("dict[str, Any]", resp.json())
        finally:
            if close:
                await client.aclose()


def build_redirect_uri(base_url: str, provider_name: str) -> str:
    """Standard relay redirect_uri for a given provider."""
    base = base_url.rstrip("/")
    return f"{base}/auth/oidc/{provider_name}/callback"


def generate_pkce_verifier() -> str:
    return token_urlsafe(64)


__all__ = [
    "OIDCClient",
    "OIDCError",
    "TokenSet",
    "code_challenge_for",
    "build_redirect_uri",
    "generate_pkce_verifier",
]
