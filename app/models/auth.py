"""Domain 1 — Identity & Auth: users, oauth_accounts, sessions, tokens."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk
from .enums import UserRole, UserStatus


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.job_seeker, nullable=False, index=True
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus), default=UserStatus.pending_verify, nullable=False, index=True
    )
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(64))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, index=True)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)

    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    profile: Mapped[Optional["Profile"]] = relationship(  # noqa: F821
        back_populates="user", uselist=False
    )


class OAuthAccount(Base):
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_user"),
    )

    oauth_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = uuid_fk("users.user_id")
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # google|linkedin|apple
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(String(2048))
    refresh_token: Mapped[Optional[str]] = mapped_column(String(2048))
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()

    user: Mapped[User] = relationship(back_populates="oauth_accounts")


class Session(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = uuid_fk("users.user_id")
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_info: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()

    user: Mapped[User] = relationship(back_populates="sessions")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    token_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = uuid_fk("users.user_id")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    token_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = uuid_fk("users.user_id")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()
