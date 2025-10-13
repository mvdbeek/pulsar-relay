"""Authentication dependencies for FastAPI."""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.jwt import decode_token
from app.auth.models import TokenPayload, User
from app.auth.storage import UserStorage

logger = logging.getLogger(__name__)

# HTTP Bearer token scheme
security = HTTPBearer()

# Global user storage instance (set during startup)
_user_storage: Optional[UserStorage] = None


def set_user_storage(storage: UserStorage) -> None:
    """Set the global user storage instance.

    Args:
        storage: User storage backend
    """
    global _user_storage
    _user_storage = storage
    logger.info("Set user storage for authentication")


def get_user_storage() -> UserStorage:
    """Get the user storage instance.

    Returns:
        User storage backend

    Raises:
        RuntimeError: If user storage not initialized
    """
    if _user_storage is None:
        raise RuntimeError("User storage not initialized")
    return _user_storage


async def get_token_payload(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenPayload:
    """Extract and validate JWT token from request.

    Args:
        credentials: HTTP authorization credentials

    Returns:
        Token payload

    Raises:
        HTTPException: If token is invalid or expired
    """
    token = credentials.credentials

    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


async def get_current_user(
    token_payload: TokenPayload = Depends(get_token_payload),
) -> User:
    """Get the current authenticated user.

    Args:
        token_payload: Validated token payload

    Returns:
        Current user

    Raises:
        HTTPException: If user not found or inactive
    """
    storage = get_user_storage()
    user = await storage.get_user_by_id(token_payload.sub)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive",
        )

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Get the current active user (alias for get_current_user).

    Args:
        current_user: Current user from token

    Returns:
        Current active user
    """
    return current_user


def require_permission(permission: str):
    """Create a dependency that requires a specific permission.

    Args:
        permission: Required permission

    Returns:
        Dependency function
    """

    async def permission_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        """Check if user has required permission.

        Args:
            current_user: Current user

        Returns:
            Current user

        Raises:
            HTTPException: If user lacks permission
        """
        if permission not in current_user.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' required",
            )
        return current_user

    return permission_checker


def require_any_permission(*permissions: str):
    """Create a dependency that requires any of the specified permissions.

    Args:
        *permissions: Required permissions (user needs at least one)

    Returns:
        Dependency function
    """

    async def permission_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        """Check if user has any of the required permissions.

        Args:
            current_user: Current user

        Returns:
            Current user

        Raises:
            HTTPException: If user lacks all permissions
        """
        if not any(perm in current_user.permissions for perm in permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of permissions {permissions} required",
            )
        return current_user

    return permission_checker
