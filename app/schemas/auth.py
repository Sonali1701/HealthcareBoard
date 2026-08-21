"""Auth schemas: registration, login, tokens, MFA."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..models.enums import UserRole, UserStatus
from .common import ORMModel

# Roles a member of the public may register as. `admin` (and anything else) is
# never self-assignable — otherwise anyone could POST role=admin and own the app.
_SELF_SIGNUP_ROLES = {UserRole.job_seeker, UserRole.recruiter, UserRole.employer}


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.job_seeker
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _no_privileged_role(cls, v: UserRole) -> UserRole:
        if v not in _SELF_SIGNUP_ROLES:
            raise ValueError("role must be one of: job_seeker, recruiter, employer")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    mfa_code: Optional[str] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access token lifetime, seconds


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    user_id: str
    email: EmailStr
    role: UserRole
    status: UserStatus
    email_verified_at: Optional[datetime] = None
    mfa_enabled: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime


class MFAEnrollResponse(BaseModel):
    secret: str
    provisioning_uri: str


class MFAVerifyRequest(BaseModel):
    code: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)
