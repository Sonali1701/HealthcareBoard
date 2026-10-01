"""Enterprise organization teams and Medhunt messaging entitlements."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class OrganizationTeam(Base):
    __tablename__ = "organization_teams"
    __table_args__ = (
        UniqueConstraint("employer_id", "name", name="uq_organization_team_name"),
        Index("ix_organization_teams_employer_id", "employer_id"),
    )

    team_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", index=True,
    )
    created_by_user_id: Mapped[Optional[str]] = uuid_fk(
        "users.user_id", nullable=True, ondelete="SET NULL",
    )
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()


class OrganizationTeamMember(Base):
    __tablename__ = "organization_team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_organization_team_member"),
        Index("ix_organization_team_members_team_id", "team_id"),
        Index("ix_organization_team_members_user_id", "user_id"),
    )

    team_member_id: Mapped[str] = uuid_pk()
    team_id: Mapped[str] = uuid_fk("organization_teams.team_id")
    user_id: Mapped[str] = uuid_fk("users.user_id")
    team_role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="member", server_default="member",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", index=True,
    )
    created_by_user_id: Mapped[Optional[str]] = uuid_fk(
        "users.user_id", nullable=True, ondelete="SET NULL",
    )
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()


class MedhuntMessagingPermission(Base):
    __tablename__ = "medhunt_messaging_permissions"
    __table_args__ = (
        UniqueConstraint("employer_id", "user_id", name="uq_medhunt_messaging_permission"),
        Index("ix_medhunt_messaging_permissions_employer_id", "employer_id"),
        Index("ix_medhunt_messaging_permissions_user_id", "user_id"),
    )

    permission_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    user_id: Mapped[str] = uuid_fk("users.user_id")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="disabled", server_default="disabled", index=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(Text)
    permission_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"),
    )
    updated_by_user_id: Mapped[Optional[str]] = uuid_fk(
        "users.user_id", nullable=True, ondelete="SET NULL",
    )
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
