"""
FastAPI dependencies for the three access states: unauthenticated (no
dependency, or get_current_user_optional returning None), user
(get_current_user — any valid token), and admin (require_admin — a valid
token whose stored role is 'admin').

Role is read fresh from the users table on every request (get_user_by_id),
not trusted from the token's embedded role claim — a promotion/demotion
via tools/create_admin.py takes effect on the user's very next request
instead of waiting for their token to expire and get reissued.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException, status

from app.auth.security import TokenError, decode_token
from app.config.config import get_settings
from app.storage.users import get_user_by_id


def _extract_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return token


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _extract_token(authorization)
    settings = get_settings()
    try:
        payload = decode_token(token, settings.jwt_secret)
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token subject") from exc
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    return user


def get_current_user_optional(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
    """For endpoints that behave differently for unauthenticated vs
    authenticated but don't require login — returns None instead of 401."""
    if not authorization:
        return None
    try:
        return get_current_user(authorization)
    except HTTPException:
        return None


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
