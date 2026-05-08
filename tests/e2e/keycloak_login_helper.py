"""Drive Keycloak's HTML login form so e2e tests can complete an OIDC sign-in.

Keycloak's login page contains a ``<form action="...">`` whose URL embeds the
session state. We GET the page, parse out the action URL, then POST username
+ password to it. Keycloak responds with a 302 redirect to the relay's
configured ``redirect_uri`` carrying ``code=`` + ``state=``.
"""

from __future__ import annotations

import re
from html import unescape

import httpx


_FORM_ACTION_RE = re.compile(
    r'<form[^>]*\bid=["\']kc-form-login["\'][^>]*\baction=["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)


def _extract_form_action(html: str) -> str:
    match = _FORM_ACTION_RE.search(html)
    if not match:
        raise RuntimeError("could not find Keycloak login form action URL in HTML")
    return unescape(match.group(1))


def login_via_keycloak(
    *,
    authorization_url: str,
    username: str,
    password: str,
    follow_relay_callback: bool = False,
) -> httpx.Response:
    """Walk the auth-code flow through Keycloak and return the final response.

    ``authorization_url`` is the URL the relay redirected the operator to
    (i.e. the ``Location`` header of ``GET /auth/oidc/keycloak/login``).

    By default we stop at the redirect *out* of Keycloak (so the caller can
    inspect the ``Location`` header to extract ``code`` + ``state``). With
    ``follow_relay_callback=True`` the caller is expected to also handle the
    callback against the relay using the returned response's location.
    """
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        # 1. GET auth URL → Keycloak's login page (HTML).
        login_page = client.get(authorization_url, follow_redirects=True)
        if login_page.status_code != 200:
            raise RuntimeError(
                f"Expected Keycloak login page, got HTTP {login_page.status_code}: {login_page.text[:200]!r}"
            )
        action_url = _extract_form_action(login_page.text)

        # 2. POST credentials to the form action URL.
        submit = client.post(
            action_url,
            data={
                "username": username,
                "password": password,
                "credentialId": "",
            },
            cookies=login_page.cookies,
        )
        if submit.status_code not in (302, 303):
            raise RuntimeError(
                f"Unexpected response from Keycloak login form: {submit.status_code} {submit.text[:300]!r}"
            )
        # 3. Optionally also follow the redirect into the relay's callback.
        if follow_relay_callback:
            cb_url = submit.headers["location"]
            return client.get(cb_url, cookies=submit.cookies, follow_redirects=False)
        return submit


def code_and_state_from_callback(location: str) -> tuple[str, str]:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    if "code" not in qs or "state" not in qs:
        raise RuntimeError(f"Callback URL missing code/state: {location}")
    return qs["code"][0], qs["state"][0]


__all__ = ["login_via_keycloak", "code_and_state_from_callback"]
