"""Team-aware organization authorization and messaging entitlement helpers."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (
    Employer,
    EmployerMember,
    MedhuntMessagingPermission,
    MedhuntSmsSender,
    OrganizationTeam,
    OrganizationTeamMember,
    User,
)
from . import org_roles

TEAM_ROLES = {"manager", "member"}
TEAM_MEMBER_STATUSES = {"active", "paused"}
MESSAGING_STATUSES = {"enabled", "paused", "disabled"}


def organization_member_ids(db: Session, employer: Employer) -> set[str]:
    ids = set(db.scalars(select(EmployerMember.user_id).where(
        EmployerMember.employer_id == employer.employer_id,
    )).all())
    ids.add(employer.owner_user_id)
    return ids


def accessible_team_ids(db: Session, employer: Employer, user: User) -> set[str]:
    role = org_roles.role_of(db, employer, user)
    if role in {"owner", "admin"}:
        return set(db.scalars(select(OrganizationTeam.team_id).where(
            OrganizationTeam.employer_id == employer.employer_id,
            OrganizationTeam.status == "active",
        )).all())
    return set(db.scalars(
        select(OrganizationTeamMember.team_id)
        .join(OrganizationTeam, OrganizationTeam.team_id == OrganizationTeamMember.team_id)
        .where(
            OrganizationTeam.employer_id == employer.employer_id,
            OrganizationTeam.status == "active",
            OrganizationTeamMember.user_id == user.user_id,
            OrganizationTeamMember.status == "active",
        )
    ).all())


def managed_team_ids(db: Session, employer: Employer, user: User) -> set[str]:
    role = org_roles.role_of(db, employer, user)
    if role in {"owner", "admin"}:
        return accessible_team_ids(db, employer, user)
    if role != "manager":
        return set()
    return set(db.scalars(
        select(OrganizationTeamMember.team_id)
        .join(OrganizationTeam, OrganizationTeam.team_id == OrganizationTeamMember.team_id)
        .where(
            OrganizationTeam.employer_id == employer.employer_id,
            OrganizationTeam.status == "active",
            OrganizationTeamMember.user_id == user.user_id,
            OrganizationTeamMember.team_role == "manager",
            OrganizationTeamMember.status == "active",
        )
    ).all())


def scoped_member_ids(db: Session, employer: Employer, user: User) -> set[str]:
    role = org_roles.role_of(db, employer, user)
    if role in {"owner", "admin"}:
        return organization_member_ids(db, employer)
    if role == "manager":
        teams = managed_team_ids(db, employer, user)
        if not teams:
            return {user.user_id}
        ids = set(db.scalars(select(OrganizationTeamMember.user_id).where(
            OrganizationTeamMember.team_id.in_(teams),
            OrganizationTeamMember.status == "active",
        )).all())
        ids.add(user.user_id)
        return ids
    return {user.user_id}


def manageable_member_ids(db: Session, employer: Employer, user: User) -> set[str]:
    """Members shown in administration, including paused memberships."""
    role = org_roles.role_of(db, employer, user)
    if role in {"owner", "admin"}:
        return organization_member_ids(db, employer)
    if role == "manager":
        teams = managed_team_ids(db, employer, user)
        if not teams:
            return {user.user_id}
        ids = set(db.scalars(select(OrganizationTeamMember.user_id).where(
            OrganizationTeamMember.team_id.in_(teams),
        )).all())
        ids.add(user.user_id)
        return ids
    return {user.user_id}


def can_manage_member(db: Session, employer: Employer, actor: User, target_user_id: str) -> bool:
    role = org_roles.role_of(db, employer, actor)
    if role == "owner":
        return target_user_id in organization_member_ids(db, employer)
    if role == "admin":
        return (target_user_id != employer.owner_user_id
                and target_user_id in organization_member_ids(db, employer))
    if role != "manager" or target_user_id in {actor.user_id, employer.owner_user_id}:
        return False
    target_membership = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer.employer_id,
        EmployerMember.user_id == target_user_id,
    ))
    if not target_membership or org_roles.normalize_role(target_membership.member_role) != "recruiter":
        return False
    return target_user_id in manageable_member_ids(db, employer, actor)


def messaging_permission(db: Session, employer_id: str, user_id: str) -> MedhuntMessagingPermission | None:
    return db.scalar(select(MedhuntMessagingPermission).where(
        MedhuntMessagingPermission.employer_id == employer_id,
        MedhuntMessagingPermission.user_id == user_id,
    ))


def messaging_status(db: Session, employer: Employer, user_id: str) -> str:
    permission = messaging_permission(db, employer.employer_id, user_id)
    if permission:
        return permission.status
    # Preserve current installations during rollout. The migration also
    # backfills these rows, while newly synchronized Zoom identities can be
    # created as disabled until an admin or team manager enables them.
    existing_sender = db.scalar(select(MedhuntSmsSender.sender_id).where(
        MedhuntSmsSender.employer_id == employer.employer_id,
        MedhuntSmsSender.user_id == user_id,
    ))
    return "enabled" if existing_sender else "disabled"


def active_organization_for_sender(db: Session, user_id: str) -> list[Employer]:
    return db.scalars(select(Employer).where(or_(
        Employer.owner_user_id == user_id,
        Employer.employer_id.in_(select(EmployerMember.employer_id).where(
            EmployerMember.user_id == user_id,
        )),
    ))).all()
