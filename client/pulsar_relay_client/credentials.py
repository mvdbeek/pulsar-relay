"""Relay credentials storage.

The daemon's on-disk path (``CredentialsFile``) is the canonical store:
``pulsar-config --login`` writes the refresh token there, and the relay
auth manager rotates it on every refresh. For embedded use — e.g. Galaxy's
multi-tenant BYOC runner, which holds rotated tokens in its own vault
rather than on disk — ``InMemoryCredentialsStore`` exposes the same
``load`` / ``save`` / ``exists`` shape with a callback fired on every
rotation so the caller can persist where they like.
"""

import json
import logging
import os
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, cast, runtime_checkable

log = logging.getLogger(__name__)


SAFE_MODE = 0o600


@runtime_checkable
class CredentialsStore(Protocol):
    """Minimal contract for any refresh-token credentials backing store.

    The default implementations in this module (``CredentialsFile`` and
    ``InMemoryCredentialsStore``) satisfy this Protocol. Embedders that
    persist refresh tokens elsewhere — Galaxy's BYOC vault, a secrets
    manager, etc. — can implement this Protocol directly without
    inheriting from either concrete class.
    """

    #: Human-readable identifier used only in log messages. For a file-backed
    #: store this is the absolute path; for an in-memory store it is a
    #: sentinel label.
    path: str

    def exists(self) -> bool: ...

    def load(self) -> Optional[dict[str, Any]]: ...

    def save(self, data: dict[str, Any]) -> None: ...


class CredentialsFile:
    """Wrapper around a JSON credentials file with mode-checking and atomic writes."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> Optional[dict[str, Any]]:
        """Read the credentials file. Returns ``None`` if it does not exist.

        Logs a warning if the file is more permissive than mode 0600.
        """
        if not self.exists():
            return None
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
        except OSError as exc:
            log.warning("Failed to stat credentials file %s: %s", self.path, exc)
            mode = None
        if mode is not None and (mode & 0o077):
            log.warning(
                "Relay credentials file %s has mode 0%o; recommended is 0%o.",
                self.path,
                mode,
                SAFE_MODE,
            )
        try:
            with open(self.path, encoding="utf-8") as f:
                return cast(dict[str, Any], json.load(f))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to read relay credentials at %s: %s", self.path, exc)
            return None

    def save(self, data: dict[str, Any]) -> None:
        """Atomically write the credentials file with mode 0600.

        Writes to ``path.tmp``, fsyncs, sets perms, then renames over the
        original. The temp file inherits the destination's directory.
        """
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".pulsar-relay-cred-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, SAFE_MODE)
            os.replace(tmp_path, self.path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


class InMemoryCredentialsStore:
    """In-memory equivalent of :class:`CredentialsFile`.

    Used by callers (e.g. Galaxy's multi-tenant BYOC Pulsar runner) that
    hold the relay refresh token in their own secret store. ``save`` writes
    to memory and fires an optional ``on_save`` callback so the caller can
    durably persist the rotated token before the next process picks it up.

    Exposes a ``path`` attribute purely for log messages; the value is a
    sentinel and does not refer to a real file.
    """

    def __init__(
        self,
        relay_url: str,
        refresh_token: str,
        on_save: Optional[Callable[[dict[str, Any]], None]] = None,
        label: str = "<in-memory>",
    ) -> None:
        self.path = label
        self._on_save = on_save
        self._data: dict[str, Any] = {
            "relay_url": relay_url,
            "refresh_token": refresh_token,
            "issued_at": utcnow_iso(),
        }

    def exists(self) -> bool:
        return bool(self._data.get("refresh_token"))

    def load(self) -> Optional[dict[str, Any]]:
        return dict(self._data) if self._data.get("refresh_token") else None

    def save(self, data: dict[str, Any]) -> None:
        self._data = dict(data)
        if self._on_save is not None:
            try:
                self._on_save(dict(data))
            except Exception:
                # The token has been rotated and is held in memory; the
                # caller-supplied persistence callback failed. Log loudly
                # but keep serving the new token to the live process.
                log.exception("on_save callback failed for refresh-token rotation at %s", self.path)


def utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
