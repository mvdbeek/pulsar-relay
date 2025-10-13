"""JWT token utilities."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from jwt import DecodeError, ExpiredSignatureError, InvalidTokenError
from passlib.context import CryptContext

from app.auth.models import TokenPayload, User

logger = logging.getLogger(__name__)

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT settings (should be loaded from config in production)
SECRET_KEY = "your-secret-key-here-change-in-production"  # Should be in environment variable
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60  # 1 hour


def hash_password(password: str) -> str:
    """Hash a password.

    Args:
        password: Plain text password

    Returns:
        Hashed password
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash.

    Args:
        plain_password: Plain text password
        hashed_password: Hashed password

    Returns:
        True if password matches, False otherwise
    """
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(user: User, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token.

    Args:
        user: User to create token for
        expires_delta: Optional custom expiration time

    Returns:
        JWT token string
    """
    now = datetime.now(timezone.utc)

    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {
        "sub": user.user_id,
        "username": user.username,
        "permissions": user.permissions,
        "exp": int(expire.timestamp()),
        "iat": int(now.timestamp()),
    }

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    logger.debug(f"Created JWT token for user {user.username}")

    return encoded_jwt


def decode_token(token: str) -> Optional[TokenPayload]:
    """Decode and validate a JWT token.

    Args:
        token: JWT token string

    Returns:
        TokenPayload if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        token_data = TokenPayload(**payload)
        return token_data
    except ExpiredSignatureError:
        logger.warning("JWT token has expired")
        return None
    except (DecodeError, InvalidTokenError) as e:
        logger.warning(f"JWT validation failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Error decoding JWT: {e}")
        return None


def get_token_expiration_seconds() -> int:
    """Get the token expiration time in seconds.

    Returns:
        Token expiration time in seconds
    """
    return ACCESS_TOKEN_EXPIRE_MINUTES * 60
