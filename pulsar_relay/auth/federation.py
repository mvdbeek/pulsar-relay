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


_USERNAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._@+-]")


def _sanitize_username(raw: str) -> str:
    """Squeeze characters not allowed by typical username rules."""
    cleaned = _USERNAME_SAFE_RE.sub("-", raw).strip("-")
    # Match pulsar-relay's UserCreate constraint: 3-50 chars.
    if len(cleaned) < 3:
        cleaned = (cleaned + "-user")[:50]
    return cleaned[:50]


async def _allocate_unique_username(
    storage: UserStorage, base: str, *, provider_name: str, sub: str
) -> str:
    """Pick a username, suffixing on collision.

    Order: base → ``base-{provider}`` → ``base-{provider}-{shortsub}`` →
    ``base-{provider}-{shortsub}-{random}``.
    """
    candidate = _sanitize_username(base)
    if await storage.get_user_by_username(candidate) is None:
        return candidate

    candidate2 = _sanitize_username(f"{base}-{provider_name}")
    if await storage.get_user_by_username(candidate2) is None:
        return candidate2

    short_sub = re.sub(r"[^A-Za-z0-9]", "", sub)[:8] or "x"
    candidate3 = _sanitize_username(f"{base}-{provider_name}-{short_sub}")
    if await storage.get_user_by_username(candidate3) is None:
        return candidate3

    # Fallback: append random suffix until we find a free slot.
    for _ in range(5):
        rand = uuid4().hex[:6]
        candidate4 = _sanitize_username(f"{base}-{provider_name}-{short_sub}-{rand}")
        if await storage.get_user_by_username(candidate4) is None:
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
    username_source = (
        _claim_str(claims, provider_config.claim_username)
        or _claim_str(claims, "preferred_username")
        or _claim_str(claims, "email")
        or sub
    )
    username = await _allocate_unique_username(
        storage, username_source, provider_name=provider_name, sub=sub
    )

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


__all__ = ["login_or_provision_oidc_user"]
