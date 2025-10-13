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


class LoginRequest(BaseModel):
    """Login request."""

    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration time in seconds")
    user: UserPublic = Field(..., description="User information")


class TokenPayload(BaseModel):
    """JWT token payload."""

    sub: str = Field(..., description="Subject (user_id)")
    username: str = Field(..., description="Username")
    permissions: list[str] = Field(default_factory=list)
    exp: int = Field(..., description="Expiration timestamp")
    iat: int = Field(..., description="Issued at timestamp")
