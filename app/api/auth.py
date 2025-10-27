"""Authentication endpoints."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth.dependencies import (
    get_current_user,
    get_user_storage,
    require_permission,
)
from app.auth.jwt import (
    create_access_token,
    get_token_expiration_seconds,
    hash_password,
    verify_password,
)
from app.auth.models import (
    TokenResponse,
    User,
    UserCreate,
    UserPublic,
    UserUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenResponse:
    """Authenticate user and return JWT token (OAuth2 compatible).

    This endpoint uses OAuth2 password flow for compatibility with FastAPI's
    built-in OpenAPI authentication UI.

    Args:
        form_data: OAuth2 password request form (username and password)

    Returns:
        JWT token and user information

    Raises:
        HTTPException: If credentials are invalid
    """
    storage = get_user_storage()

    # Get user by username
    user = await storage.get_user_by_username(form_data.username)
    if not user:
        logger.warning(f"Login attempt for non-existent user: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify password
    if not verify_password(form_data.password, user.hashed_password):
        logger.warning(f"Invalid password for user: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if user is active
    if not user.is_active:
        logger.warning(f"Login attempt for inactive user: {form_data.username}")
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
            owned_topics=user.owned_topics,
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
        owned_topics=current_user.owned_topics,
    )


@router.get("/users", response_model=list[UserPublic])
async def list_users(
    current_user: User = Depends(require_permission("admin")),
) -> list[UserPublic]:
    """List all users (admin only).

    Args:
        current_user: Current authenticated admin user

    Returns:
        List of all users
    """
    storage = get_user_storage()
    users = await storage.list_users()

    # Convert to UserPublic
    return [
        UserPublic(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            is_active=user.is_active,
            created_at=user.created_at,
            permissions=user.permissions,
            owned_topics=user.owned_topics,
        )
        for user in users
    ]


@router.patch("/users/{user_id}", response_model=UserPublic)
async def update_user(
    user_id: str,
    user_update: UserUpdate,
    current_user: User = Depends(require_permission("admin")),
) -> UserPublic:
    """Update a user by ID (admin only).

    Only provided fields will be updated. All fields in the request body are optional.

    Args:
        user_id: User ID to update
        user_update: User update data (partial update)
        current_user: Current authenticated admin user

    Returns:
        Updated user information

    Raises:
        HTTPException: If user not found or update fails
    """
    storage = get_user_storage()

    # Get the existing user
    user = await storage.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID '{user_id}' not found",
        )

    # Apply updates (only non-None fields)
    update_data = user_update.model_dump(exclude_unset=True)

    if "email" in update_data:
        user.email = update_data["email"]

    if "password" in update_data:
        user.hashed_password = hash_password(update_data["password"])

    if "permissions" in update_data:
        user.permissions = update_data["permissions"]

    if "is_active" in update_data:
        user.is_active = update_data["is_active"]

    # Update the user in storage
    try:
        updated_user = await storage.update_user(user)
        logger.info(f"Admin {current_user.username} updated user {updated_user.username} ({user_id})")

        return UserPublic(
            user_id=updated_user.user_id,
            username=updated_user.username,
            email=updated_user.email,
            is_active=updated_user.is_active,
            created_at=updated_user.created_at,
            permissions=updated_user.permissions,
            owned_topics=updated_user.owned_topics,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
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


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: User = Depends(require_permission("admin")),
) -> None:
    """Delete a user by ID (admin only).

    Args:
        user_id: User ID to delete
        current_user: Current authenticated admin user

    Raises:
        HTTPException: If user cannot be deleted or not found
    """
    storage = get_user_storage()

    # Prevent admin from deleting themselves
    if user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own user account",
        )

    # Verify user exists before attempting deletion
    user_to_delete = await storage.get_user_by_id(user_id)
    if not user_to_delete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID '{user_id}' not found",
        )

    # Delete the user
    deleted = await storage.delete_user(user_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user",
        )

    logger.info(f"Admin {current_user.username} deleted user {user_to_delete.username} ({user_id})")
