"""Process-local rate limiter shared across route modules.

The :class:`slowapi.Limiter` is module-level so individual route handlers
can import it directly via ``from pulsar_relay.api.limits import limiter``
and decorate themselves without main.py needing to know about every
route's rate-limit policy.

By default the limiter uses an in-memory storage; for multi-worker
deployments operators should configure a Valkey-backed storage URI via
``PULSAR_RATE_LIMIT_STORAGE_URI``. The per-route limits below are
intentionally generous and meant as defence-in-depth — operators behind
a reverse proxy should set their own tighter limits there.
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# ``key_func`` extracts the rate-limit bucket key from each request. For
# authenticated endpoints we pair the IP with the bearer token's sub (via
# the get_current_user dependency injecting it into request.state); for
# unauthenticated endpoints we fall back to the source IP. Keeping the
# baseline as ``get_remote_address`` here means routes that have not been
# updated to populate request.state.user still get an IP-based bucket.

# Storage URI: when running multi-worker (uvicorn --workers >1), each
# worker has its own in-memory counter, so the effective rate limit is
# (N * limit). Setting PULSAR_RATE_LIMIT_STORAGE_URI to a Valkey URI
# (e.g. ``redis://:<password>@localhost:6379/1``) shares the counter.
_storage_uri = os.environ.get("PULSAR_RATE_LIMIT_STORAGE_URI", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_storage_uri,
    # ``headers_enabled=False``: slowapi's header-injection logic
    # expects a starlette ``Response`` parameter on every decorated
    # handler so it can stamp X-RateLimit-* headers on the return
    # value. Our handlers return Pydantic models / dicts; injecting
    # headers via the decorator path raises. Operators who want the
    # informational headers should run behind a reverse proxy that
    # adds them, or wire them via FastAPI's ``Response`` injection.
    headers_enabled=False,
)


__all__ = ["limiter"]
