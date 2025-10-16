"""Topic management API endpoints."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import (
    get_current_user,
    get_topic_storage,
    get_user_storage,
    require_permission,
)
from app.auth.models import (
    TopicCreate,
    TopicPermission,
    TopicPermissionGrant,
    TopicPublic,
    TopicUpdate,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])


@router.post("", response_model=TopicPublic, status_code=status.HTTP_201_CREATED)
async def create_topic(
    topic_data: TopicCreate,
    current_user: User = Depends(require_permission("write")),
) -> TopicPublic:
    """Create a new topic.

    Requires 'write' permission. User becomes the owner of the topic.

    Args:
        topic_data: Topic creation data
        current_user: Current authenticated user

    Returns:
        Created topic information

    Raises:
        HTTPException: If topic already exists or creation fails
    """
    topic_storage = get_topic_storage()
    user_storage = get_user_storage()

    try:
        topic = await topic_storage.create_topic(current_user.user_id, topic_data)

        # Update user's owned_topics list
        if topic.topic_name not in current_user.owned_topics:
            current_user.owned_topics.append(topic.topic_name)
            await user_storage.update_user(current_user)

        logger.info(f"Topic created: {topic.topic_name} by user {current_user.username}")

        return TopicPublic(
            topic_id=topic.topic_id,
            topic_name=topic.topic_name,
            owner_id=topic.owner_id,
            is_public=topic.is_public,
            created_at=topic.created_at,
            description=topic.description,
            allowed_user_ids=topic.allowed_user_ids,  # Owner can see all
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("", response_model=list[TopicPublic])
async def list_topics(
    current_user: User = Depends(get_current_user),
) -> list[TopicPublic]:
    """List all topics accessible to the current user.

    Returns topics the user owns or has been granted access to.

    Args:
        current_user: Current authenticated user

    Returns:
        List of accessible topics
    """
    topic_storage = get_topic_storage()

    # Admins see all owned topics, others see their accessible topics
    if "admin" in current_user.permissions:
        topics = await topic_storage.list_owned_topics(current_user.user_id)
    else:
        topics = await topic_storage.list_user_topics(current_user.user_id)

    return [
        TopicPublic(
            topic_id=topic.topic_id,
            topic_name=topic.topic_name,
            owner_id=topic.owner_id,
            is_public=topic.is_public,
            created_at=topic.created_at,
            description=topic.description,
            # Only show allowed_user_ids to owner
            allowed_user_ids=topic.allowed_user_ids if topic.owner_id == current_user.user_id else None,
        )
        for topic in topics
    ]


@router.get("/{topic_name}", response_model=TopicPublic)
async def get_topic(
    topic_name: str,
    current_user: User = Depends(get_current_user),
) -> TopicPublic:
    """Get details of a specific topic.

    Args:
        topic_name: Topic name
        current_user: Current authenticated user

    Returns:
        Topic information

    Raises:
        HTTPException: If topic not found or access denied
    """
    topic_storage = get_topic_storage()

    topic = await topic_storage.get_topic(topic_name)
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topic '{topic_name}' not found",
        )

    # Check if user has access
    can_access = await topic_storage.user_can_access(
        topic_name=topic_name,
        user_id=current_user.user_id,
        permission_type="read",
        user_permissions=current_user.permissions,
    )

    if not can_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to topic '{topic_name}'",
        )

    return TopicPublic(
        topic_id=topic.topic_id,
        topic_name=topic.topic_name,
        owner_id=topic.owner_id,
        is_public=topic.is_public,
        created_at=topic.created_at,
        description=topic.description,
        # Only show allowed_user_ids to owner
        allowed_user_ids=topic.allowed_user_ids if topic.owner_id == current_user.user_id else None,
    )


@router.put("/{topic_name}", response_model=TopicPublic)
async def update_topic(
    topic_name: str,
    update_data: TopicUpdate,
    current_user: User = Depends(get_current_user),
) -> TopicPublic:
    """Update topic metadata (owner only).

    Args:
        topic_name: Topic name
        update_data: Update data
        current_user: Current authenticated user

    Returns:
        Updated topic information

    Raises:
        HTTPException: If topic not found or not owner
    """
    topic_storage = get_topic_storage()

    topic = await topic_storage.get_topic(topic_name)
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topic '{topic_name}' not found",
        )

    # Only owner or admin can update
    if topic.owner_id != current_user.user_id and "admin" not in current_user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the topic owner can update it",
        )

    updated_topic = await topic_storage.update_topic(
        topic_name=topic_name,
        is_public=update_data.is_public,
        description=update_data.description,
    )

    if not updated_topic:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update topic",
        )

    logger.info(f"Topic updated: {topic_name} by user {current_user.username}")

    return TopicPublic(
        topic_id=updated_topic.topic_id,
        topic_name=updated_topic.topic_name,
        owner_id=updated_topic.owner_id,
        is_public=updated_topic.is_public,
        created_at=updated_topic.created_at,
        description=updated_topic.description,
        allowed_user_ids=updated_topic.allowed_user_ids,
    )


@router.delete("/{topic_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(
    topic_name: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a topic (owner only).

    This also deletes all messages in the topic.

    Args:
        topic_name: Topic name
        current_user: Current authenticated user

    Raises:
        HTTPException: If topic not found or not owner
    """
    topic_storage = get_topic_storage()
    user_storage = get_user_storage()

    topic = await topic_storage.get_topic(topic_name)
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topic '{topic_name}' not found",
        )

    # Only owner or admin can delete
    if topic.owner_id != current_user.user_id and "admin" not in current_user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the topic owner can delete it",
        )

    # Delete topic
    deleted = await topic_storage.delete_topic(topic_name)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete topic",
        )

    # Update user's owned_topics list
    if topic_name in current_user.owned_topics:
        current_user.owned_topics.remove(topic_name)
        await user_storage.update_user(current_user)

    logger.info(f"Topic deleted: {topic_name} by user {current_user.username}")


@router.post("/{topic_name}/permissions", response_model=TopicPermission, status_code=status.HTTP_201_CREATED)
async def grant_topic_access(
    topic_name: str,
    permission_grant: TopicPermissionGrant,
    current_user: User = Depends(get_current_user),
) -> TopicPermission:
    """Grant a user access to a topic (owner only).

    Args:
        topic_name: Topic name
        permission_grant: Permission grant data (user_id or username)
        current_user: Current authenticated user

    Returns:
        Permission grant record

    Raises:
        HTTPException: If topic not found, not owner, or user not found
    """
    from datetime import datetime, timezone

    topic_storage = get_topic_storage()
    user_storage = get_user_storage()

    topic = await topic_storage.get_topic(topic_name)
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topic '{topic_name}' not found",
        )

    # Only owner or admin can grant access
    if topic.owner_id != current_user.user_id and "admin" not in current_user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the topic owner can grant access",
        )

    # Get user to grant access to
    target_user = None
    if permission_grant.user_id:
        target_user = await user_storage.get_user_by_id(permission_grant.user_id)
    elif permission_grant.username:
        target_user = await user_storage.get_user_by_username(permission_grant.username)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either user_id or username must be provided",
        )

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Grant access
    try:
        await topic_storage.grant_access(topic_name, target_user.user_id)
        logger.info(f"Granted access to topic '{topic_name}' for user {target_user.username}")

        return TopicPermission(
            topic_name=topic_name,
            user_id=target_user.user_id,
            username=target_user.username,
            granted_at=datetime.now(timezone.utc),
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete("/{topic_name}/permissions/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_topic_access(
    topic_name: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Revoke a user's access to a topic (owner only).

    Args:
        topic_name: Topic name
        user_id: User ID to revoke access from
        current_user: Current authenticated user

    Raises:
        HTTPException: If topic not found or not owner
    """
    topic_storage = get_topic_storage()

    topic = await topic_storage.get_topic(topic_name)
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topic '{topic_name}' not found",
        )

    # Only owner or admin can revoke access
    if topic.owner_id != current_user.user_id and "admin" not in current_user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the topic owner can revoke access",
        )

    # Revoke access
    revoked = await topic_storage.revoke_access(topic_name, user_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User does not have access to this topic",
        )

    logger.info(f"Revoked access to topic '{topic_name}' for user {user_id}")


@router.get("/{topic_name}/permissions", response_model=list[TopicPermission])
async def list_topic_permissions(
    topic_name: str,
    current_user: User = Depends(get_current_user),
) -> list[TopicPermission]:
    """List users with access to a topic (owner only).

    Args:
        topic_name: Topic name
        current_user: Current authenticated user

    Returns:
        List of users with access

    Raises:
        HTTPException: If topic not found or not owner
    """
    from datetime import datetime, timezone

    topic_storage = get_topic_storage()
    user_storage = get_user_storage()

    topic = await topic_storage.get_topic(topic_name)
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topic '{topic_name}' not found",
        )

    # Only owner or admin can list permissions
    if topic.owner_id != current_user.user_id and "admin" not in current_user.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the topic owner can list permissions",
        )

    # Get usernames for all allowed users
    permissions = []
    for user_id in topic.allowed_user_ids:
        user = await user_storage.get_user_by_id(user_id)
        if user:
            permissions.append(
                TopicPermission(
                    topic_name=topic_name,
                    user_id=user_id,
                    username=user.username,
                    granted_at=datetime.now(timezone.utc),  # We don't track grant time currently
                )
            )

    return permissions


@router.get("/stats", response_model=dict[str, Any])
async def get_topic_stats(
    current_user: User = Depends(require_permission("admin")),
) -> dict[str, Any]:
    """Get topic statistics (admin only).

    Args:
        current_user: Current authenticated admin user

    Returns:
        Topic statistics
    """
    topic_storage = get_topic_storage()
    return await topic_storage.get_stats()
