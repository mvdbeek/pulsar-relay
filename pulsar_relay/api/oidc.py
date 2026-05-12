"""OpenID Connect authorization-code endpoints (relay as Relying Party)."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from pulsar_relay.auth.dependencies import (
    get_device_code_storage,
    get_oidc_clients,
    get_oidc_state_storage,
    get_refresh_token_storage,
    get_user_storage,
)
from pulsar_relay.auth.federation import login_or_provision_oidc_user
from pulsar_relay.auth.jwt import create_access_token, get_token_expiration_seconds
from pulsar_relay.auth.models import TokenResponse
from pulsar_relay.auth.oidc_client import OIDCError, build_redirect_uri
from pulsar_relay.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oidc", tags=["authentication"])


class OIDCProviderSummary(BaseModel):
    """Public per-provider info for the CLI / future web UI."""

    name: str
    display_name: str
    login_url: str


@router.get("/providers", response_model=list[OIDCProviderSummary])
async def list_providers() -> list[OIDCProviderSummary]:
    """List configured OIDC providers (public, used by clients to render buttons)."""
    if not settings.oidc.enabled:
        return []
    base = settings.oidc.base_url or ""
    base = base.rstrip("/")
    return [
        OIDCProviderSummary(
            name=name,
            display_name=cfg.display_name,
            login_url=f"{base}/auth/oidc/{name}/login",
        )
        for name, cfg in settings.oidc.providers.items()
    ]


@router.get("/{provider}/login")
async def start_oidc_login(
    provider: str,
    request: Request,
    next: str | None = None,
    device_user_code: str | None = None,
) -> RedirectResponse:
    """Begin an OIDC authorization-code flow for a provider.

    Generates state + PKCE verifier + nonce, persists them, and 302s the user
    to the provider. ``device_user_code`` (when present) bridges this sign-in
    to a pending device-flow session.
    """
    if not settings.oidc.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC disabled")

    clients = get_oidc_clients()
    client = clients.get(provider)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown OIDC provider: {provider}")

    base_url = settings.oidc.base_url or str(request.base_url).rstrip("/")
    redirect_uri = build_redirect_uri(base_url, provider)

    state_storage = get_oidc_state_storage()
    state_record = await state_storage.create(
        provider_name=provider,
        redirect_uri=redirect_uri,
        ttl=timedelta(seconds=settings.oidc.state_ttl_seconds),
        next_url=next,
        device_user_code=device_user_code,
    )

    try:
        auth_url = await client.build_authorization_url(
            redirect_uri=redirect_uri,
            state=state_record.state,
            nonce=state_record.nonce,
            code_verifier=state_record.code_verifier,
        )
    except OIDCError as exc:
        logger.exception("Failed to build auth URL for provider %s: %s", provider, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="OIDC provider unreachable") from exc

    return RedirectResponse(auth_url, status_code=status.HTTP_302_FOUND)


_DEVICE_APPROVED_HTML = """<!doctype html>
<html><head><title>Sign-in complete</title>
<style>body{font-family:system-ui;text-align:center;padding:3em;max-width:36em;margin:auto}</style>
</head><body>
<h1>Sign-in complete</h1>
<p>You can close this tab and return to your terminal.
The pulsar daemon will pick up the credential automatically.</p>
</body></html>
"""


@router.get("/{provider}/callback")
async def oidc_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Receive an authorization-code redirect from the IdP.

    Validates state + PKCE + ID token, provisions or links the user, and
    either:
    - returns ``TokenResponse`` JSON for a vanilla browser sign-in, or
    - approves a pending device-flow session and renders a "you may close
      this tab" page.
    """
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OIDC error: {error}: {error_description or ''}".strip(),
        )
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or state")

    state_storage = get_oidc_state_storage()
    state_record = await state_storage.consume(state)
    if state_record is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired state")
    if state_record.provider_name != provider:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="State/provider mismatch")

    clients = get_oidc_clients()
    client = clients.get(provider)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown OIDC provider")

    try:
        token_set = await client.exchange_code(
            code=code,
            redirect_uri=state_record.redirect_uri,
            code_verifier=state_record.code_verifier,
        )
        claims = await client.validate_id_token(token_set.id_token, nonce=state_record.nonce)
    except OIDCError as exc:
        logger.warning("OIDC callback failed for provider=%s: %s", provider, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"OIDC validation failed: {exc}") from exc

    user_storage = get_user_storage()
    user = await login_or_provision_oidc_user(
        user_storage,
        provider_name=provider,
        provider_config=settings.oidc.providers[provider],
        oidc_config=settings.oidc,
        claims=claims,
    )

    # Bridge to a pending device-flow session if applicable.
    if state_record.device_user_code:
        device_storage = get_device_code_storage()
        approved = await device_storage.approve(state_record.device_user_code, user.user_id)
        if approved is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Device-flow session expired or already consumed",
            )
        return HTMLResponse(_DEVICE_APPROVED_HTML)

    # Plain browser flow — return tokens directly. (Refresh-token issuance is
    # added in the token-endpoint task; for now we issue an access token only.)
    access_token = create_access_token(user)
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=get_token_expiration_seconds(),
    )


@router.post("/{provider}/swagger-token")
async def swagger_token_exchange(
    provider: str,
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    code_verifier: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),  # noqa: ARG001 — accepted but ignored
    pair: bool = Form(False),
) -> JSONResponse:
    """Exchange an OIDC authorization code for a *relay-issued* JWT.

    Used by the Swagger UI on ``/docs`` so an operator can authenticate via
    the configured OIDC provider and get a relay token they can wield against
    the rest of the API. The relay still only validates its own JWTs — this
    endpoint is the bridge.

    Unlike the standard ``/auth/oidc/{provider}/callback`` (which we drove via
    a server-side ``state``/``nonce`` record), Swagger UI generates its own
    state and nonce client-side, so there is no in-memory record to consult.
    We rely on PKCE + ID-token signature/issuer/audience/exp to authenticate
    the request.
    """
    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": grant_type},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not settings.oidc.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC disabled")
    clients = get_oidc_clients()
    oidc_client = clients.get(provider)
    if oidc_client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown OIDC provider: {provider}")
    if not code_verifier:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "PKCE code_verifier required",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    expected_client_id = settings.oidc.providers[provider].client_id
    if client_id and client_id != expected_client_id:
        return JSONResponse(
            {"error": "invalid_client"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        token_set = await oidc_client.exchange_code(
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        # Swagger UI generated the nonce; we can't verify it server-side
        # without storing it. PKCE + sig + iss + aud + exp keep us safe.
        claims = await oidc_client.validate_id_token(token_set.id_token, nonce=None)
    except OIDCError as exc:
        logger.warning("Swagger OIDC token exchange failed for provider=%s: %s", provider, exc)
        return JSONResponse(
            {"error": "invalid_grant", "error_description": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user_storage = get_user_storage()
    user = await login_or_provision_oidc_user(
        user_storage,
        provider_name=provider,
        provider_config=settings.oidc.providers[provider],
        oidc_config=settings.oidc,
        claims=claims,
    )

    access_token = create_access_token(user)
    refresh_storage = get_refresh_token_storage()
    _, refresh_wire = await refresh_storage.create(
        user_id=user.user_id,
        ttl=timedelta(days=settings.refresh_token_ttl_days),
        client_hint="swagger-ui",
    )
    body: dict[str, object] = {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": get_token_expiration_seconds(),
        "refresh_token": refresh_wire,
    }
    if pair:
        # Same rationale as on the device-flow endpoint: issue a second
        # independent refresh token so the caller can hand it to a
        # delegate (e.g. Galaxy) without sharing the rotation chain.
        _, secondary_wire = await refresh_storage.create(
            user_id=user.user_id,
            ttl=timedelta(days=settings.refresh_token_ttl_days),
            client_hint="swagger-ui-secondary",
        )
        body["refresh_token_secondary"] = secondary_wire
    return JSONResponse(body)
