"""JWT token utilities."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from jwt import DecodeError, ExpiredSignatureError, InvalidTokenError
from pwdlib import PasswordHash

from pulsar_relay.auth.models import TokenPayload, User

logger = logging.getLogger(__name__)

# Password hashing context
pwd_context = PasswordHash.recommended()

ALGORITHM = "HS256"


def _get_secret_key() -> str:
    """Resolve the JWT signing secret from settings at call time.

    Reading at call time (rather than module import) ensures env var changes
    and test overrides are honored.
    """
    from pulsar_relay.config import settings

    return settings.jwt_secret_key


def _get_expire_minutes() -> int:
    """Resolve the access-token lifetime from settings at call time."""
    from pulsar_relay.config import settings

    return settings.access_token_expire_minutes


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
        expire = now + timedelta(minutes=_get_expire_minutes())

    # ``jti`` is a per-token UUID so /auth/logout can deny-list this
    # specific token without revoking other concurrent sessions for the
    # same user.
    to_encode = {
        "sub": user.user_id,
        "username": user.username,
        "permissions": user.permissions,
        "exp": int(expire.timestamp()),
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }

    encoded_jwt = jwt.encode(to_encode, _get_secret_key(), algorithm=ALGORITHM)
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
        payload = jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM])
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
    return _get_expire_minutes() * 60
