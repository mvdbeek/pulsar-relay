"""Authentication endpoints."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import (
    get_current_user,
    get_user_storage,
    require_permission,
)
from app.auth.jwt import (
    create_access_token,
    get_token_expiration_seconds,
    verify_password,
)
from app.auth.models import (
    LoginRequest,
    TokenResponse,
    User,
    UserCreate,
    UserPublic,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
async def login(login_request: LoginRequest) -> TokenResponse:
    """Authenticate user and return JWT token.

    Args:
        login_request: Login credentials

    Returns:
        JWT token and user information

    Raises:
        HTTPException: If credentials are invalid
    """
    storage = get_user_storage()

    # Get user by username
    user = await storage.get_user_by_username(login_request.username)
    if not user:
        logger.warning(f"Login attempt for non-existent user: {login_request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    # Verify password
    if not verify_password(login_request.password, user.hashed_password):
        logger.warning(f"Invalid password for user: {login_request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    # Check if user is active
    if not user.is_active:
        logger.warning(f"Login attempt for inactive user: {login_request.username}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    # Create access token
    access_token = create_access_token(user)

    logger.info(f"User logged in successfully: {user.username}")

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=get_token_expiration_seconds(),
        user=UserPublic(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            is_active=user.is_active,
            created_at=user.created_at,
            permissions=user.permissions,
        ),
    )


@router.post(
    "/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    user_data: UserCreate,
    current_user: User = Depends(require_permission("admin")),
) -> UserPublic:
    """Register a new user (admin only).

    Args:
        user_data: User registration data
        current_user: Current authenticated admin user

    Returns:
        Created user information

    Raises:
        HTTPException: If username already exists or validation fails
    """
    storage = get_user_storage()

    try:
        user = await storage.create_user(user_data)
        logger.info(f"User {user.username} registered by admin {current_user.username}")

        return UserPublic(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            is_active=user.is_active,
            created_at=user.created_at,
            permissions=user.permissions,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/me", response_model=UserPublic)
async def get_current_user_info(
    current_user: User = Depends(get_current_user),
) -> UserPublic:
    """Get current user information.

    Args:
        current_user: Current authenticated user

    Returns:
        Current user information
    """
    return UserPublic(
        user_id=current_user.user_id,
        username=current_user.username,
        email=current_user.email,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        permissions=current_user.permissions,
    )


@router.get("/users/stats")
async def get_user_stats(
    current_user: User = Depends(require_permission("admin")),
) -> dict[str, Any]:
    """Get user statistics (admin only).

    Args:
        current_user: Current authenticated admin user

    Returns:
        User statistics
    """
    storage = get_user_storage()

    if hasattr(storage, "get_stats"):
        return storage.get_stats()

    return {"error": "Statistics not available for this storage backend"}
