"""Authentication dependencies for FastAPI."""

import logging
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.auth.jwt import decode_token
from app.auth.models import TokenPayload, User
from app.auth.storage import UserStorage

if TYPE_CHECKING:
    from app.auth.topic_storage import TopicStorage

logger = logging.getLogger(__name__)

# OAuth2 password bearer scheme for OpenAPI
# tokenUrl points to the login endpoint
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login",
    scheme_name="OAuth2PasswordBearer",
    description="JWT Bearer token authentication",
    auto_error=True,
)

# Global storage instances (set during startup)
_user_storage: Optional[UserStorage] = None
_topic_storage: Optional["TopicStorage"] = None  # Forward reference to avoid circular import


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


def set_topic_storage(storage) -> None:
    """Set the global topic storage instance.

    Args:
        storage: Topic storage backend
    """
    global _topic_storage
    _topic_storage = storage
    logger.info("Set topic storage for authorization")


def get_topic_storage() -> "TopicStorage":
    """Get the topic storage instance.

    Returns:
        Topic storage backend

    Raises:
        RuntimeError: If topic storage not initialized
    """
    if _topic_storage is None:
        raise RuntimeError("Topic storage not initialized")
    return _topic_storage


async def get_token_payload(
    token: str = Depends(oauth2_scheme),
) -> TokenPayload:
    """Extract and validate JWT token from request.

    Args:
        token: JWT token from Authorization header

    Returns:
        Token payload

    Raises:
        HTTPException: If token is invalid or expired
    """
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
    from app.core.cache import user_cache

    # Check cache first to reduce database load during high concurrency
    cache_key = f"user:{token_payload.sub}"
    user = user_cache.get(cache_key)

    if user is None:
        # Cache miss - fetch from storage
        storage = get_user_storage()
        user = await storage.get_user_by_id(token_payload.sub)

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Cache the user for future requests
        user_cache.set(cache_key, user)

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


def require_topic_access(topic: str, permission_type: Literal["read", "write"]):
    """Create a dependency that requires access to a specific topic.

    Args:
        topic: Topic name
        permission_type: Type of access required ("read" or "write")

    Returns:
        Dependency function
    """

    async def topic_access_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        """Check if user has access to the topic.

        Args:
            current_user: Current user

        Returns:
            Current user

        Raises:
            HTTPException: If user lacks access to topic
        """
        topic_storage = get_topic_storage()

        # Check if user can access the topic
        can_access = await topic_storage.user_can_access(
            topic_name=topic,
            user_id=current_user.user_id,
            permission_type=permission_type,
            user_permissions=current_user.permissions,
        )

        if not can_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to topic '{topic}' (requires {permission_type} permission)",
            )

        return current_user

    return topic_access_checker


async def get_or_create_topic(topic_name: str, current_user: User):
    """Get or create a topic, setting the current user as owner.

    This function handles concurrent creation attempts gracefully by retrying
    the get operation if the create fails due to the topic already existing.

    Args:
        topic_name: Topic name
        current_user: Current user

    Returns:
        Topic instance

    Raises:
        HTTPException: If topic creation fails
    """
    from app.auth.models import TopicCreate

    topic_storage = get_topic_storage()

    # Try to get existing topic
    topic = await topic_storage.get_topic(topic_name)

    if topic:
        return topic

    # Topic doesn't exist - try to create it with current user as owner
    try:
        topic_data = TopicCreate(
            topic_name=topic_name,
            is_public=False,  # Default to private
            description=f"Auto-created topic by {current_user.username}",
        )
        topic = await topic_storage.create_topic(current_user.user_id, topic_data)
        logger.info(f"Auto-created topic '{topic_name}' for user {current_user.username}")

        # Update user's owned_topics list
        if topic_name not in current_user.owned_topics:
            current_user.owned_topics.append(topic_name)
            user_storage = get_user_storage()
            await user_storage.update_user(current_user)

        return topic
    except ValueError as e:
        # Topic was created by another concurrent request - retry the get
        if "already exists" in str(e):
            logger.debug(f"Topic '{topic_name}' was created by concurrent request, retrying get")
            topic = await topic_storage.get_topic(topic_name)
            if topic:
                return topic
        # Still couldn't get it, raise the error
        logger.exception(f"Failed to get or create topic '{topic_name}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get or create topic: {str(e)}",
        )
    except Exception as e:
        logger.exception(f"Failed to create topic '{topic_name}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create topic: {str(e)}",
        )
