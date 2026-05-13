"""Federate an OIDC sign-in to a local relay user.

The flow:
1. If we've seen this ``(issuer, sub)`` before, return the linked user.
2. Otherwise, auto-provision a new user with the configured default permissions.
   The username is derived from a configured claim (default: ``email``); on
   collision we suffix ``-{provider}`` then ``-{provider}-{shortsub}``.

Email-based linking to a pre-existing password account is intentionally not
implemented in v1 — it requires either a slow scan or a new email index, and
operators told us greenfield deployments don't need it. Admins can link
identities manually via the storage API if needed.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from pulsar_relay.auth.models import FederatedIdentity, Permission, User
from pulsar_relay.auth.storage import UserStorage
from pulsar_relay.config import OIDCConfig, OIDCProviderConfig

logger = logging.getLogger(__name__)


class FederationConflictError(Exception):
    """Raised when an OIDC sign-in would collide with a pre-existing
    non-federated (password) account.

    The legacy "suffix on collision" behaviour silently created a *new*
    user (``base-{provider}``) when a local password account already
    owned the base username. That meant an attacker who controlled
    ``preferred_username=admin`` at a trusted IdP could sign in as
    ``admin-keycloak`` — distinct from the intended ``admin`` account
    on paper, but indistinguishable to a hurried operator. We now
    refuse the sign-in instead and let the operator decide.
    """


_USERNAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._@+-]")


def _sanitize_username(raw: str) -> str:
    """Squeeze characters not allowed by typical username rules."""
    cleaned = _USERNAME_SAFE_RE.sub("-", raw).strip("-")
    # Match pulsar-relay's UserCreate constraint: 3-50 chars.
    if len(cleaned) < 3:
        cleaned = (cleaned + "-user")[:50]
    return cleaned[:50]


def _user_is_federated(user: User) -> bool:
    """Return True if ``user`` already has at least one federated identity.

    OIDC sign-in is allowed to collide with an existing *federated* user
    (we'd never reach the collision path for them — the ``(iss, sub)``
    lookup would have found them first), but we refuse to collide with
    a non-federated (password) account.
    """
    return bool(user.federated_identities)


async def _allocate_unique_username(storage: UserStorage, base: str, *, provider_name: str, sub: str) -> str:
    """Pick a username, suffixing on collision against *other federated*
    users only.

    Order: base → ``base-{provider}`` → ``base-{provider}-{shortsub}`` →
    ``base-{provider}-{shortsub}-{random}``.

    Raises :class:`FederationConflictError` if any candidate username
    collides with a non-federated (password-protected) account. Closes
    Auth H#3 from the security review: previously, signing in via an
    IdP with ``preferred_username=admin`` would silently provision
    ``admin-keycloak`` if a local ``admin`` already existed.
    """

    def _check_or_collide(candidate: str, existing: User | None) -> bool:
        if existing is None:
            return True
        if not _user_is_federated(existing):
            raise FederationConflictError(
                f"OIDC username {candidate!r} collides with a non-federated local account. "
                "Refusing to auto-provision. An admin must reconcile manually."
            )
        return False

    candidate = _sanitize_username(base)
    if _check_or_collide(candidate, await storage.get_user_by_username(candidate)):
        return candidate

    candidate2 = _sanitize_username(f"{base}-{provider_name}")
    if _check_or_collide(candidate2, await storage.get_user_by_username(candidate2)):
        return candidate2

    short_sub = re.sub(r"[^A-Za-z0-9]", "", sub)[:8] or "x"
    candidate3 = _sanitize_username(f"{base}-{provider_name}-{short_sub}")
    if _check_or_collide(candidate3, await storage.get_user_by_username(candidate3)):
        return candidate3

    # Fallback: append random suffix until we find a free slot.
    for _ in range(5):
        rand = uuid4().hex[:6]
        candidate4 = _sanitize_username(f"{base}-{provider_name}-{short_sub}-{rand}")
        if _check_or_collide(candidate4, await storage.get_user_by_username(candidate4)):
            return candidate4
    raise RuntimeError("could not allocate a unique username for OIDC sign-in")


def _claim_str(claims: dict[str, Any], key: str) -> str | None:
    val = claims.get(key)
    if val is None:
        return None
    s = str(val).strip()
    return s or None


async def login_or_provision_oidc_user(
    storage: UserStorage,
    *,
    provider_name: str,
    provider_config: OIDCProviderConfig,
    oidc_config: OIDCConfig,
    claims: dict[str, Any],
) -> User:
    """Translate a validated set of OIDC claims into a relay ``User``.

    Idempotent: re-running with the same ``(iss, sub)`` returns the existing
    user record without modification.
    """
    issuer = _claim_str(claims, "iss")
    sub = _claim_str(claims, provider_config.claim_sub)
    if not issuer or not sub:
        raise ValueError("OIDC claims must include 'iss' and the configured sub claim")

    existing = await storage.get_user_by_federated_identity(issuer, sub)
    if existing is not None:
        return existing

    # Provision a new user.
    #
    # Email-as-username only when the IdP has positively asserted that
    # the email has been verified. Without this gate, an IdP that
    # accepts unverified email claims would let an attacker register
    # arbitrary ``email=victim@example.com`` and squat the victim's
    # username on first sign-in. Closes Auth H#3 (the email-claim
    # portion).
    email_verified = claims.get("email_verified") is True

    def _source_or_skip(claim_name: str) -> str | None:
        value = _claim_str(claims, claim_name)
        if value is None:
            return None
        if claim_name == "email" and not email_verified:
            logger.warning(
                "OIDC provision: refusing to use unverified email claim as username (provider=%s, sub=%s)",
                provider_name,
                sub,
            )
            return None
        return value

    username_source = (
        _source_or_skip(provider_config.claim_username)
        or _source_or_skip("preferred_username")
        or _source_or_skip("email")
        or sub
    )
    username = await _allocate_unique_username(storage, username_source, provider_name=provider_name, sub=sub)

    email = _claim_str(claims, provider_config.claim_email)
    permissions: list[Permission] = list(oidc_config.default_permissions)

    user = User(
        user_id=str(uuid4()),
        username=username,
        email=email,
        hashed_password=None,
        permissions=permissions,
        federated_identities=[
            FederatedIdentity(
                issuer=issuer,
                sub=sub,
                provider_name=provider_name,
                email=email,
            )
        ],
    )
    await storage.put_user(user)

    logger.info(
        "Auto-provisioned OIDC user %s via provider=%s (iss=%s, sub=%s)",
        user.username,
        provider_name,
        issuer,
        sub,
    )
    return user


__all__ = ["FederationConflictError", "login_or_provision_oidc_user"]
