"""Authentication models and schemas."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from pulsar_relay.models import TopicName


def _utcnow() -> datetime:
    """Timezone-aware UTC now (datetime.utcnow is deprecated)."""
    return datetime.now(tz=timezone.utc)


# Define valid permission values
Permission = Literal["admin", "read", "write"]


class FederatedIdentity(BaseModel):
    """A user's identity at an upstream OIDC provider.

    A user may have multiple federated identities (e.g. linked Google + Keycloak).
    The ``(issuer, sub)`` pair is the immutable identifier from the IdP.
    """

    issuer: str = Field(..., description="OIDC issuer URL (iss claim)")
    sub: str = Field(..., description="Stable subject identifier from the IdP (sub claim)")
    provider_name: str = Field(..., description="Local provider config name (e.g. 'google', 'keycloak')")
    email: Optional[str] = Field(None, description="Email claim at the time of linking")
    linked_at: datetime = Field(default_factory=_utcnow)


class User(BaseModel):
    """User model."""

    user_id: str = Field(..., description="Unique user identifier")
    username: str = Field(..., description="Username")
    email: Optional[str] = Field(None, description="User email")
    # Optional: OIDC-only users have no local password.
    hashed_password: Optional[str] = Field(None, description="Hashed password (None for OIDC-only users)")
    is_active: bool = Field(default=True, description="Whether user is active")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    permissions: list[Permission] = Field(default_factory=list, description="User permissions (admin, read, write)")
    owned_topics: list[str] = Field(default_factory=list, description="Topics owned by this user")
    federated_identities: list[FederatedIdentity] = Field(
        default_factory=list,
        description="Linked OIDC identities for this user",
    )


class UserCreate(BaseModel):
    """User creation request."""

    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[str] = Field(None)
    password: str = Field(..., min_length=8)
    permissions: list[Permission] = Field(default_factory=list, description="User permissions (admin, read, write)")


class UserUpdate(BaseModel):
    """User update request (partial update).

    All fields are optional. Only provided fields will be updated.
    """

    email: Optional[str] = Field(None, description="User email")
    password: Optional[str] = Field(None, min_length=8, description="New password (will be hashed)")
    permissions: Optional[list[Permission]] = Field(None, description="User permissions (admin, read, write)")
    is_active: Optional[bool] = Field(None, description="Whether user is active")


class UserPublic(BaseModel):
    """Public user information (no sensitive data)."""

    user_id: str
    username: str
    email: Optional[str]
    is_active: bool
    created_at: datetime
    permissions: list[Permission]
    owned_topics: list[str]


class LoginRequest(BaseModel):
    """Login request."""

    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class TokenResponse(BaseModel):
    """JWT token response (OAuth2 compliant).

    Following OAuth2 spec, only includes standard fields plus a relay-specific
    ``refresh_token_secondary`` for the pair-issuance extension used by
    Galaxy BYOC bootstrap. To get user info, clients should call /auth/me
    with the token.
    """

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration time in seconds")
    refresh_token: Optional[str] = Field(
        None,
        description="Long-lived refresh token (rotates on use). Optional for backwards compatibility.",
    )
    refresh_token_secondary: Optional[str] = Field(
        None,
        description=(
            "Second independent refresh token issued when the caller requested "
            "``pair=true`` at device-flow or swagger-token exchange. Rotates on "
            "its own chain — neither sibling's rotation invalidates the other."
        ),
    )


class TokenPayload(BaseModel):
    """JWT token payload."""

    sub: str = Field(..., description="Subject (user_id)")
    username: str = Field(..., description="Username")
    permissions: list[Permission] = Field(default_factory=list)
    exp: int = Field(..., description="Expiration timestamp")
    iat: int = Field(..., description="Issued at timestamp")
    jti: Optional[str] = Field(
        None,
        description="JWT ID — unique per token. Set by create_access_token "
        "so /auth/logout can deny-list the specific token. Older tokens "
        "issued before this field landed have jti=None and cannot be "
        "deny-listed (they expire on their own).",
    )


class Topic(BaseModel):
    """Topic model.

    The ``is_public`` and ``allowed_user_ids`` fields that this model
    previously carried have been removed: per-user topic namespacing
    (Phase 3c, API H#5) made the wire contract unable to address any
    topic outside the bearer's own namespace, so neither flag had a
    reachable code path. The cross-user-sharing feature would need a
    new wire mechanism (e.g. ``?owner=...``) to be reintroduced
    coherently.
    """

    topic_id: str = Field(..., description="Unique topic identifier")
    topic_name: str = Field(..., description="Topic name")
    owner_id: str = Field(..., description="User ID of the topic owner")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    description: Optional[str] = Field(None, description="Topic description")


class TopicCreate(BaseModel):
    """Topic creation request."""

    topic_name: TopicName = Field(..., description="Topic name")
    description: Optional[str] = Field(None, max_length=500, description="Topic description")


class TopicUpdate(BaseModel):
    """Topic update request."""

    description: Optional[str] = Field(None, max_length=500, description="Topic description")


class TopicPublic(BaseModel):
    """Public topic information."""

    topic_id: str
    topic_name: str
    owner_id: str
    created_at: datetime
    description: Optional[str]


# --- Refresh tokens, device flow, OIDC state ---

RefreshTokenRevokedReason = Literal["rotated", "logout", "replay", "admin", "expired"]


class RefreshToken(BaseModel):
    """Persisted refresh-token record.

    The wire token is ``f"{jti}.{secret}"`` where ``secret`` is a random 32-byte
    base64url string. We persist only ``secret_hash`` (sha256 hex) and look up
    by ``jti``. ``parent_jti`` chains rotated tokens together so a replay can
    revoke the entire family.
    """

    jti: str = Field(..., description="Token identifier (used for lookup)")
    user_id: str = Field(..., description="User this token authenticates")
    secret_hash: str = Field(..., description="sha256 hex of the secret half")
    parent_jti: Optional[str] = Field(None, description="Previous token in the rotation chain")
    issued_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(..., description="Absolute expiry")
    last_used_at: Optional[datetime] = Field(None, description="Last refresh time")
    revoked: bool = Field(default=False)
    revoked_reason: Optional[RefreshTokenRevokedReason] = None
    client_hint: Optional[str] = Field(None, description="Free-form descriptor of the issuing client")


DeviceCodeStatus = Literal["pending", "approved", "denied", "expired"]


class DeviceCode(BaseModel):
    """RFC 8628 device authorization grant record.

    ``device_code_hash`` is sha256 of the device_code presented to the daemon.
    ``user_code`` is the short, human-typeable code shown to the operator.
    """

    device_code_hash: str = Field(..., description="sha256 hex of the device_code")
    user_code: str = Field(..., description="Short user-facing code (shown to operator)")
    verification_uri: str = Field(..., description="Where the user goes to approve")
    verification_uri_complete: str = Field(..., description="URI with user_code prefilled")
    issued_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(..., description="Absolute expiry (issued_at + ~10 min)")
    interval: int = Field(default=5, description="Minimum poll interval in seconds (RFC 8628)")
    last_polled_at: Optional[datetime] = None
    status: DeviceCodeStatus = "pending"
    user_id: Optional[str] = Field(None, description="Set once an OIDC sign-in approves the code")
    client_hint: Optional[str] = Field(None, description="Free-form descriptor of the requesting client")
    pair: bool = Field(
        default=False,
        description=(
            "If True, the daemon requested a pair of independent refresh tokens at "
            "issuance time. Used by Galaxy BYOC bootstrap: the daemon keeps one, "
            "hands the other to Galaxy. Each rotates on its own chain so neither "
            "client locks the other out."
        ),
    )


class OIDCStateRecord(BaseModel):
    """Short-lived per-request OIDC state, PKCE, and nonce.

    Created when a user (or device flow) starts an authorization-code request,
    consumed when the IdP redirects back to ``/auth/oidc/{provider}/callback``.
    """

    state: str = Field(..., description="Opaque state value used as the CSRF token")
    provider_name: str = Field(..., description="Configured provider this state belongs to")
    code_verifier: str = Field(..., description="PKCE code_verifier (kept server-side)")
    nonce: str = Field(..., description="Nonce sent in auth request, verified in ID token")
    redirect_uri: str = Field(..., description="The exact redirect_uri passed to the IdP")
    next_url: Optional[str] = Field(None, description="Where to send the user after success")
    device_user_code: Optional[str] = Field(
        None,
        description="If set, this state is bridging an in-flight device-flow approval",
    )
    issued_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(..., description="Absolute expiry (typically 10 min)")
