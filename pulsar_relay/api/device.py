"""RFC 8628 Device Authorization Grant endpoints.

Used by headless daemons (e.g. ``pulsar-config --login``) to bootstrap a
long-lived refresh token without ever holding the user's password. The daemon:

1. POSTs ``/auth/device/code`` to obtain a ``device_code`` + ``user_code``.
2. Prints ``verification_uri`` + ``user_code`` to the operator.
3. Polls ``/auth/device/token`` until the operator completes a browser
   sign-in via one of the configured OIDC providers (or denies).

Approval is tied to the OIDC callback in ``pulsar_relay/api/oidc.py``.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from pulsar_relay.auth.dependencies import (
    get_device_code_storage,
    get_refresh_token_storage,
    get_user_storage,
)
from pulsar_relay.auth.jwt import create_access_token, get_token_expiration_seconds
from pulsar_relay.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/device", tags=["authentication"])


_RFC8628_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _rfc8628_error(error: str, *, status_code: int = 400, description: str | None = None) -> JSONResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status_code)


@router.post("/code")
async def request_device_code(
    request: Request,
    client_hint: str | None = Form(None),
    scope: str | None = Form(None),  # noqa: ARG001 — accepted but ignored in v1
    pair: bool = Form(False),
) -> JSONResponse:
    """RFC 8628 §3.1: issue a ``device_code`` + ``user_code`` pair.

    ``pair=true`` is a relay extension: when set, the subsequent token
    poll returns *two* independent refresh tokens for the same user
    (``refresh_token`` + ``refresh_token_secondary``). Used by Galaxy BYOC
    bootstrap so the user's host and Galaxy each get a rotation-independent
    token — neither can lock the other out via single-use rotation.
    """
    if not settings.oidc.enabled or not settings.oidc.providers:
        # Without OIDC there's no way for an operator to approve a device
        # session — short-circuit with a clear error.
        return _rfc8628_error(
            "device_authorization_unavailable",
            description="No OIDC providers configured; device flow requires at least one.",
        )

    storage = get_device_code_storage()
    base_url = settings.oidc.base_url or str(request.base_url).rstrip("/")
    verification_uri = f"{base_url.rstrip('/')}/auth/device"
    verification_uri_complete_template = f"{base_url.rstrip('/')}/auth/device?user_code={{user_code}}"

    record, device_code = await storage.create(
        verification_uri=verification_uri,
        verification_uri_complete_template=verification_uri_complete_template,
        ttl=timedelta(seconds=settings.device_code_ttl_seconds),
        interval=settings.device_code_poll_interval,
        client_hint=client_hint or request.headers.get("user-agent"),
        pair=pair,
    )

    return JSONResponse(
        {
            "device_code": device_code,
            "user_code": record.user_code,
            "verification_uri": record.verification_uri,
            "verification_uri_complete": record.verification_uri_complete,
            "expires_in": int((record.expires_at - _utcnow()).total_seconds()),
            "interval": record.interval,
        }
    )


@router.post("/token")
async def poll_device_token(
    grant_type: str = Form(...),
    device_code: str = Form(...),
    client_id: str | None = Form(None),  # noqa: ARG001 — RFC8628 allows it; we don't validate in v1
) -> JSONResponse:
    """RFC 8628 §3.4: daemon polling endpoint."""
    if grant_type != _RFC8628_GRANT:
        return _rfc8628_error("unsupported_grant_type", description="Use device_code grant.")

    storage = get_device_code_storage()
    record = await storage.get_by_device_code(device_code)
    if record is None:
        return _rfc8628_error("expired_token", description="Unknown device_code")

    now = _utcnow()
    if record.expires_at <= now or record.status == "expired":
        if record.status != "expired":
            record.status = "expired"
            await storage.update(record)
        return _rfc8628_error("expired_token")

    if record.status == "denied":
        await storage.consume(device_code)
        return _rfc8628_error("access_denied")

    # Rate-limit per RFC 8628 §3.5: enforce ``interval`` between polls.
    if record.last_polled_at is not None:
        elapsed = (now - record.last_polled_at).total_seconds()
        if elapsed < record.interval:
            return _rfc8628_error("slow_down")

    record.last_polled_at = now

    if record.status == "pending":
        await storage.update(record)
        return _rfc8628_error("authorization_pending")

    if record.status == "approved":
        if not record.user_id:  # defensive: approval must set user_id
            return _rfc8628_error("expired_token")
        user = await get_user_storage().get_user_by_id(record.user_id)
        if user is None:
            return _rfc8628_error("expired_token")

        # Single-use: drop the device session before returning tokens.
        await storage.consume(device_code)

        access_token = create_access_token(user)
        refresh_storage = get_refresh_token_storage()
        _, refresh_wire = await refresh_storage.create(
            user_id=user.user_id,
            ttl=timedelta(days=settings.refresh_token_ttl_days),
            client_hint=record.client_hint,
        )
        body: dict[str, object] = {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": get_token_expiration_seconds(),
            "refresh_token": refresh_wire,
        }
        if record.pair:
            # Issue a second, independent refresh token for the same user
            # (no ``parent_jti`` linkage). Each chain rotates separately so
            # the daemon and its delegate (typically Galaxy) don't collide.
            _, secondary_wire = await refresh_storage.create(
                user_id=user.user_id,
                ttl=timedelta(days=settings.refresh_token_ttl_days),
                client_hint=(record.client_hint or "") + "-secondary" if record.client_hint else "secondary",
            )
            body["refresh_token_secondary"] = secondary_wire
        return JSONResponse(body)

    return _rfc8628_error("expired_token", description="Unexpected device-code status")


_DEVICE_PAGE = """<!doctype html>
<html><head><title>Approve device</title>
<style>
 body {{
   font-family: system-ui, sans-serif;
   max-width: 36em; margin: 3em auto; padding: 0 1em;
 }}
 .code {{
   font-family: ui-monospace, monospace; font-size: 1.4em;
   background: #eee; padding: 0.4em 0.8em; border-radius: 6px;
   display: inline-block;
 }}
 .providers a {{
   display: inline-block; margin: 0.4em 0.4em 0.4em 0;
   padding: 0.6em 1.2em; border: 1px solid #888; border-radius: 6px;
   text-decoration: none; color: #222;
 }}
 .hint {{ color: #555; font-size: 0.9em }}
</style></head><body>
<h1>Approve device sign-in</h1>
<p>Confirming code: <span class="code">{user_code}</span></p>
{hint_html}
<p>Choose how you'd like to sign in:</p>
<div class="providers">{provider_buttons}</div>
</body></html>
"""

_DEVICE_PROMPT_HTML = """<!doctype html>
<html><head><title>Enter user code</title>
<style>body{font-family:system-ui;max-width:30em;margin:3em auto;padding:0 1em}
input{font-family:ui-monospace,monospace;font-size:1.2em;padding:0.4em;width:100%;box-sizing:border-box}
button{font-size:1em;padding:0.6em 1.2em;margin-top:0.8em}</style></head><body>
<h1>Enter your user code</h1>
<form method="get" action="">
  <input type="text" name="user_code" placeholder="XXXX-XXXX" autofocus>
  <button type="submit">Continue</button>
</form>
</body></html>
"""

_DEVICE_NOT_FOUND_HTML = """<!doctype html>
<html><body style="font-family:system-ui;max-width:30em;margin:3em auto;padding:0 1em">
<h1>Code not recognised</h1>
<p>That user code is invalid, already used, or has expired. Re-run
<code>pulsar-config --login</code> to start over.</p>
</body></html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def device_landing(user_code: str | None = None) -> HTMLResponse:
    """Operator-facing approval page.

    With no ``user_code`` we render a small form prompting for it. Otherwise
    we look up the device session and render provider-selection buttons.
    """
    if not settings.oidc.enabled or not settings.oidc.providers:
        return HTMLResponse(
            "<h1>OIDC is not configured on this relay.</h1>",
            status_code=503,
        )
    if not user_code:
        return HTMLResponse(_DEVICE_PROMPT_HTML)

    storage = get_device_code_storage()
    record = await storage.get_by_user_code(user_code.upper())
    if record is None or record.status != "pending" or record.expires_at <= _utcnow():
        return HTMLResponse(_DEVICE_NOT_FOUND_HTML, status_code=404)

    # ``record.client_hint`` is set from the device-flow request body or
    # the User-Agent header — both attacker-controlled. ``cfg.display_name``
    # is operator-supplied but goes through the same template so we
    # escape it too. Escaping ``record.user_code`` is defensive even
    # though it is constrained to alnum upstream.
    safe_hint = html.escape(record.client_hint, quote=True) if record.client_hint else ""
    hint_html = f'<p class="hint">Requested by: <code>{safe_hint}</code></p>' if safe_hint else ""
    buttons = "".join(
        f'<a href="/auth/oidc/{html.escape(name, quote=True)}/login?'
        f'device_user_code={html.escape(record.user_code, quote=True)}">'
        f"{html.escape(cfg.display_name, quote=True)}</a>"
        for name, cfg in settings.oidc.providers.items()
    )
    return HTMLResponse(
        _DEVICE_PAGE.format(
            user_code=html.escape(record.user_code, quote=True),
            hint_html=hint_html,
            provider_buttons=buttons,
        )
    )


@router.post("/deny")
async def deny_device_code(user_code: str = Form(...)) -> JSONResponse:
    storage = get_device_code_storage()
    record = await storage.deny(user_code)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user_code")
    return JSONResponse({"status": "denied"})
