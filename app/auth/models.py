"""Authentication models and schemas."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class User(BaseModel):
    """User model."""

    user_id: str = Field(..., description="Unique user identifier")
    username: str = Field(..., description="Username")
    email: Optional[str] = Field(None, description="User email")
    hashed_password: str = Field(..., description="Hashed password")
    is_active: bool = Field(default=True, description="Whether user is active")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    permissions: list[str] = Field(default_factory=list, description="User permissions")
    owned_topics: list[str] = Field(default_factory=list, description="Topics owned by this user")


class UserCreate(BaseModel):
    """User creation request."""

    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[str] = Field(None)
    password: str = Field(..., min_length=8)
    permissions: list[str] = Field(default_factory=list)


class UserPublic(BaseModel):
    """Public user information (no sensitive data)."""

    user_id: str
    username: str
    email: Optional[str]
    is_active: bool
    created_at: datetime
    permissions: list[str]
    owned_topics: list[str]


class LoginRequest(BaseModel):
    """Login request."""

    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class TokenResponse(BaseModel):
    """JWT token response (OAuth2 compliant).

    Following OAuth2 spec, only includes standard fields.
    To get user info, clients should call /auth/me with the token.
    """

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration time in seconds")


class TokenPayload(BaseModel):
    """JWT token payload."""

    sub: str = Field(..., description="Subject (user_id)")
    username: str = Field(..., description="Username")
    permissions: list[str] = Field(default_factory=list)
    exp: int = Field(..., description="Expiration timestamp")
    iat: int = Field(..., description="Issued at timestamp")


class Topic(BaseModel):
    """Topic model."""

    topic_id: str = Field(..., description="Unique topic identifier")
    topic_name: str = Field(..., description="Topic name")
    owner_id: str = Field(..., description="User ID of the topic owner")
    is_public: bool = Field(default=False, description="Whether topic is publicly accessible for reading")
    allowed_user_ids: list[str] = Field(default_factory=list, description="User IDs with access to this topic")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    description: Optional[str] = Field(None, description="Topic description")


class TopicCreate(BaseModel):
    """Topic creation request."""

    topic_name: str = Field(..., min_length=1, max_length=255, description="Topic name")
    is_public: bool = Field(default=False, description="Whether topic is publicly accessible for reading")
    description: Optional[str] = Field(None, max_length=500, description="Topic description")


class TopicUpdate(BaseModel):
    """Topic update request."""

    is_public: Optional[bool] = Field(None, description="Whether topic is publicly accessible for reading")
    description: Optional[str] = Field(None, max_length=500, description="Topic description")


class TopicPublic(BaseModel):
    """Public topic information."""

    topic_id: str
    topic_name: str
    owner_id: str
    is_public: bool
    created_at: datetime
    description: Optional[str]
    # Only shown to owner
    allowed_user_ids: Optional[list[str]] = None


class TopicPermissionGrant(BaseModel):
    """Grant access to a topic."""

    user_id: Optional[str] = Field(None, description="User ID to grant access to")
    username: Optional[str] = Field(None, description="Username to grant access to (alternative to user_id)")


class TopicPermission(BaseModel):
    """Topic permission record."""

    topic_name: str
    user_id: str
    username: str
    granted_at: datetime
