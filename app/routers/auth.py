"""
Auth endpoints — signup/login/me/logout.

This app (the Ionic frontend) is the internal ops console for PredictX,
not the public product — end users get a separate app/site. So every
account created here is trusted staff, and every signup lands as
role='admin' outright; there is no lesser role reachable through this
API. require_admin route guards on the ops endpoints stay in place as
real protection against unauthenticated/outside callers, they just
always pass for anyone who can sign up here. tools/create_admin.py still
works for creating/promoting accounts out-of-band if ever needed.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.auth.dependencies import get_current_user
from app.auth.security import encode_token
from app.config.config import get_settings
from app.storage.users import UserExistsError, create_user, verify_credentials

router = APIRouter(prefix="/auth", tags=["auth"])

# Deliberately a plain str + regex rather than pydantic's EmailStr, which
# pulls in the separate email-validator package — not worth a new
# dependency for a check this simple.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=100)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value


def _issue_token(user: dict) -> str:
    settings = get_settings()
    return encode_token(
        {"sub": str(user["id"]), "role": user["role"]},
        settings.jwt_secret,
        expires_in_seconds=settings.jwt_expiry_hours * 3600,
    )


@router.post("/signup")
def signup(body: SignupRequest):
    try:
        # Admin outright -- see module docstring. This app has no
        # unprivileged role to land new accounts in.
        user = create_user(body.email, body.password, display_name=body.display_name, role="admin")
    except UserExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"status": "success", "token": _issue_token(user), "user": user}


@router.post("/login")
def login(body: LoginRequest):
    user = verify_credentials(body.email, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    return {"status": "success", "token": _issue_token(user), "user": user}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"status": "success", "user": user}


@router.post("/logout")
def logout():
    # Stateless JWT — nothing to invalidate server-side. The client just
    # discards its token. If a real logout-before-expiry guarantee is
    # needed later, add a token-blacklist table keyed by a jti claim.
    return {"status": "success"}
