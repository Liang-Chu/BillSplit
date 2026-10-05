import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from threading import Lock

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.ids import utcnow
from app.models import RefreshToken, User

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_access_token(user_id: str) -> str:
    s = get_settings()
    now = utcnow()
    payload = {"sub": user_id, "type": "access", "iat": now, "exp": now + timedelta(minutes=s.access_token_minutes)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def issue_tokens(db: Session, user: User) -> dict:
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    db.add(
        RefreshToken(user_id=user.id, token_hash=_sha256(raw), expires_at=utcnow() + timedelta(days=s.refresh_token_days))
    )
    db.flush()
    return {
        "access_token": create_access_token(user.id),
        "refresh_token": raw,
        "token_type": "bearer",
        "expires_in": s.access_token_minutes * 60,
    }


def rotate_refresh_token(db: Session, raw: str) -> User:
    """Validates and revokes a refresh token. Reuse of a revoked token revokes the whole family."""
    token = db.query(RefreshToken).filter_by(token_hash=_sha256(raw)).one_or_none()
    if token is None:
        raise APIError(401, "unauthorized", "Invalid refresh token")
    if token.revoked_at is not None:
        db.query(RefreshToken).filter(RefreshToken.user_id == token.user_id, RefreshToken.revoked_at.is_(None)).update(
            {RefreshToken.revoked_at: utcnow()}
        )
        db.commit()
        raise APIError(401, "unauthorized", "Refresh token reuse detected; please log in again")
    if token.expires_at < utcnow():
        raise APIError(401, "unauthorized", "Refresh token expired")
    token.revoked_at = utcnow()
    user = db.get(User, token.user_id)
    if user is None or user.disabled_at is not None:
        raise APIError(403, "account_disabled", "Account is disabled")
    return user


def revoke_refresh_token(db: Session, raw: str) -> None:
    token = db.query(RefreshToken).filter_by(token_hash=_sha256(raw)).one_or_none()
    if token and token.revoked_at is None:
        token.revoked_at = utcnow()


def _user_from_request(request: Request, db: Session) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise APIError(401, "unauthorized", "Missing bearer token")
    try:
        payload = jwt.decode(auth[7:], get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise APIError(401, "unauthorized", "Invalid or expired token")
    if payload.get("type") != "access":
        raise APIError(401, "unauthorized", "Invalid token type")
    user = db.get(User, payload.get("sub"))
    if user is None or user.is_placeholder:
        raise APIError(401, "unauthorized", "Unknown user")
    if user.disabled_at is not None:
        raise APIError(403, "account_disabled", "Account is disabled")
    return user


def current_user_any(request: Request, db: Session = Depends(get_db, scope="function")) -> User:
    """Authenticated user, even if they must change their password."""
    return _user_from_request(request, db)


def current_user(user: User = Depends(current_user_any)) -> User:
    if user.must_change_password:
        raise APIError(403, "must_change_password", "You must change your password first")
    return user


def super_admin(user: User = Depends(current_user)) -> User:
    if not user.is_super_admin:
        raise APIError(403, "forbidden", "Super admin only")
    return user


class LoginRateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        limit = get_settings().login_attempts_per_minute
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                raise APIError(429, "rate_limited", "Too many login attempts; try again in a minute")
            q.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_limiter = LoginRateLimiter()
