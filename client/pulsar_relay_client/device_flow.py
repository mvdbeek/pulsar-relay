"""RFC 8628 device-authorization-grant client for ``pulsar-config --login``.

Kept in its own module so the long-running daemon import path doesn't pull
``time.sleep`` based polling code, terminal-formatting heuristics, etc.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Callable, TextIO, cast

import requests

from .credentials import CredentialsFile, utcnow_iso

log = logging.getLogger(__name__)


class DeviceFlowError(Exception):
    """Raised when the device-flow handshake cannot complete."""


_RFC8628_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _print_banner(verification_uri: str, user_code: str, *, stream: TextIO | None = None) -> None:
    """Emit a hard-to-miss prompt to stderr."""
    out = stream if stream is not None else sys.stderr
    bar = "=" * 64
    msg = (
        f"\n{bar}\n"
        f"  Visit:  {verification_uri}\n"
        f"  Code:   {user_code}\n"
        f"\n  (Approve via your configured identity provider; this CLI will\n"
        f"   wait until the sign-in completes.)\n"
        f"{bar}\n"
    )
    out.write(msg)
    out.flush()


class RelayDeviceFlowAuthenticator:
    """Drive RFC 8628 against a relay and persist the resulting refresh token.

    Usage::

        cred = CredentialsFile("/etc/pulsar/relay_credentials.json")
        flow = RelayDeviceFlowAuthenticator("https://relay.example.org", cred)
        flow.run()
    """

    def __init__(
        self,
        relay_url: str,
        credentials_file: CredentialsFile,
        *,
        client_hint: str | None = None,
        timeout: int = 10,
        max_wait_seconds: int = 600,
        on_user_code: Callable[[str, str], None] | None = None,
        pair: bool = False,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self._credentials_file = credentials_file
        self._client_hint = client_hint or "pulsar-config"
        self._timeout = timeout
        self._max_wait = max_wait_seconds
        # When True, request a refresh-token pair (relay extension used by
        # the Galaxy BYOC bootstrap so the host and Galaxy each get an
        # independent rotation chain). The secondary token is surfaced on
        # the return value but never written to disk.
        self._pair = pair
        # Hook for tests / alternative UIs to receive (verification_uri_complete, user_code).
        self._on_user_code = on_user_code or (lambda uri, code: _print_banner(uri, code))

    def run(self) -> dict[str, Any]:
        """Execute the full handshake. Returns the persisted credentials dict."""
        device = self._request_device_code()
        self._on_user_code(device["verification_uri_complete"], device["user_code"])

        deadline = time.time() + min(int(device.get("expires_in", 600)), self._max_wait)
        interval = max(int(device.get("interval", 5)), 1)
        device_code = device["device_code"]

        while True:
            now = time.time()
            if now >= deadline:
                raise DeviceFlowError("Device-flow user code expired before the sign-in completed.")

            time.sleep(interval)
            outcome = self._poll(device_code)
            kind = outcome["kind"]

            if kind == "tokens":
                creds: dict[str, Any] = {
                    "relay_url": self.relay_url,
                    "refresh_token": outcome["refresh_token"],
                    "access_token": outcome["access_token"],
                    "expires_in": outcome.get("expires_in"),
                    "issued_at": utcnow_iso(),
                }
                # The credentials *file* only ever stores the primary token —
                # the secondary's purpose is to be handed off in-memory to a
                # delegate (e.g. Galaxy BYOC). Surface it on the return value
                # but don't persist it.
                self._credentials_file.save(creds)
                if outcome.get("refresh_token_secondary"):
                    creds["refresh_token_secondary"] = outcome["refresh_token_secondary"]
                log.info("Wrote relay credentials to %s", self._credentials_file.path)
                return creds
            if kind == "pending":
                continue
            if kind == "slow_down":
                interval += 5
                log.debug("Server requested slow_down; new interval=%ds", interval)
                continue
            if kind == "denied":
                raise DeviceFlowError("Device-flow sign-in was denied.")
            if kind == "expired":
                raise DeviceFlowError("Device code expired before the sign-in completed.")
            raise DeviceFlowError(f"Unexpected device-flow response: {outcome}")

    # ---- internals ---------------------------------------------------------

    def _request_device_code(self) -> dict[str, Any]:
        url = f"{self.relay_url}/auth/device/code"
        data = {"client_hint": self._client_hint}
        if self._pair:
            data["pair"] = "true"
        try:
            resp = requests.post(url, data=data, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise DeviceFlowError(f"Failed to request device code: {exc}") from exc
        return cast(dict[str, Any], resp.json())

    def _poll(self, device_code: str) -> dict[str, Any]:
        url = f"{self.relay_url}/auth/device/token"
        try:
            resp = requests.post(
                url,
                data={"grant_type": _RFC8628_GRANT, "device_code": device_code},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise DeviceFlowError(f"Polling failed (network): {exc}") from exc

        if resp.status_code == 200:
            body = resp.json()
            return {
                "kind": "tokens",
                "access_token": body["access_token"],
                "refresh_token": body.get("refresh_token"),
                "refresh_token_secondary": body.get("refresh_token_secondary"),
                "expires_in": body.get("expires_in"),
            }
        # Per RFC 8628 §3.5 errors come back as 4xx with an OAuth error code.
        try:
            body = resp.json()
        except ValueError:
            body = {}
        error = body.get("error", "")
        if error == "authorization_pending":
            return {"kind": "pending"}
        if error == "slow_down":
            return {"kind": "slow_down"}
        if error == "access_denied":
            return {"kind": "denied"}
        if error == "expired_token":
            return {"kind": "expired"}
        raise DeviceFlowError(f"Device-flow polling returned HTTP {resp.status_code}: {body}")


__all__ = ["RelayDeviceFlowAuthenticator", "DeviceFlowError"]
