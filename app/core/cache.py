"""In-memory caching utilities for reducing database load."""

import time
from typing import Generic, Optional, TypeVar

from app.auth.models import User

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Simple in-memory cache with TTL (time-to-live) support.

    This cache helps reduce redundant database lookups for frequently
    accessed data during high concurrency periods.
    """

    def __init__(self, ttl_seconds: float = 60.0, max_size: int = 1000):
        """Initialize TTL cache.

        Args:
            ttl_seconds: Time-to-live for cache entries in seconds
            max_size: Maximum number of entries to store (LRU eviction)
        """
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._cache: dict[str, tuple[T, float]] = {}  # key -> (value, expiry_time)

    def get(self, key: str) -> Optional[T]:
        """Get value from cache if it exists and hasn't expired.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        if key not in self._cache:
            return None

        value, expiry = self._cache[key]

        # Check if expired
        if time.time() > expiry:
            del self._cache[key]
            return None

        return value

    def set(self, key: str, value: T) -> None:
        """Store value in cache with TTL.

        Args:
            key: Cache key
            value: Value to cache
        """
        # Evict oldest entry if cache is full
        if len(self._cache) >= self.max_size and key not in self._cache:
            # Simple eviction: remove first entry
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]

        expiry = time.time() + self.ttl_seconds
        self._cache[key] = (value, expiry)

    def invalidate(self, key: str) -> None:
        """Remove entry from cache.

        Args:
            key: Cache key to invalidate
        """
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()


# Global cache instance for user lookups
# This is worker-local and doesn't need cross-worker synchronization
# since the authoritative data is in Valkey
user_cache: TTLCache[User] = TTLCache(ttl_seconds=60.0, max_size=1000)
