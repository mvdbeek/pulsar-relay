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


def _parent_dir_is_safe(directory: str) -> bool:
    """Reject parent directories that are world- or group-writable.

    On a shared host, a 0o775 parent dir lets an attacker swap the
    credentials file out from under us between the mode check and the
    read (TOCTOU). Refuse to load via such a directory rather than
    silently returning the contents (Storage H#5 / Client H#5).
    """
    try:
        st = os.lstat(directory)
    except OSError:
        # Caller will handle the resulting OSError; the load path
        # already returns None when stat fails.
        return True
    return not bool(stat.S_IMODE(st.st_mode) & 0o022)


class CredentialsFile:
    """Wrapper around a JSON credentials file with mode-checking and
    atomic writes.

    The Phase 3d hardening adds three defences against the previous
    TOCTOU risk (Storage H#5):

    * ``O_NOFOLLOW`` on read — refuse to dereference a symlink at
      the credentials path. An attacker who can write into the parent
      dir could otherwise plant a symlink pointing at an arbitrary
      file readable by the relay process.
    * Parent-directory permission check — refuse to load when the
      parent dir is group- or world-writable.
    * ``fchmod`` on the temp file BEFORE writing the secret — the
      previous code wrote first (with the process umask), then
      ``chmod``ed on the final path.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> Optional[dict[str, Any]]:
        """Read the credentials file. Returns ``None`` if absent."""
        if not self.exists():
            return None

        directory = os.path.dirname(self.path) or "."
        if not _parent_dir_is_safe(directory):
            log.error(
                "Refusing to read relay credentials at %s: parent directory %s is "
                "group- or world-writable. Tighten it to mode 0700/0750.",
                self.path,
                directory,
            )
            return None

        # Open with O_NOFOLLOW so a symlink at ``self.path`` produces
        # ELOOP instead of dereferencing an attacker-controlled target.
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            log.error("Failed to open relay credentials at %s: %s", self.path, exc)
            return None

        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode & 0o077:
                log.warning(
                    "Relay credentials file %s has mode 0%o; recommended is 0%o.",
                    self.path,
                    mode,
                    SAFE_MODE,
                )
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                # Once we've handed fd to fdopen, the with-block closes
                # it; clear the local so the finally block doesn't
                # close it twice.
                fd = -1
                return cast(dict[str, Any], json.load(f))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to read relay credentials at %s: %s", self.path, exc)
            return None
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def save(self, data: dict[str, Any]) -> None:
        """Atomically write the credentials file with mode 0600.

        Order: mkstemp (mode 0600 by default), fchmod (defence in
        depth), write body, fsync, replace, fsync parent dir.
        """
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        if not _parent_dir_is_safe(directory):
            raise OSError(
                f"Refusing to write credentials at {self.path!r}: parent directory "
                f"{directory!r} is group- or world-writable. Tighten it to mode 0700/0750."
            )
        fd, tmp_path = tempfile.mkstemp(prefix=".pulsar-relay-cred-", dir=directory)
        try:
            # fchmod the open fd before writing so the secret is never
            # observable through a wider mode.
            os.fchmod(fd, SAFE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            # Durably commit the rename so a crash doesn't lose the
            # rotated refresh token.
            try:
                dir_fd = os.open(directory, os.O_DIRECTORY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
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
