"""Organization-level role-based access control (RBAC).

A hierarchy inside each organization, so the platform admin can appoint an org
Admin who then runs the org, with a Manager tier under them and plain members
(recruiters) at the base:

    owner  >  admin  >  manager  >  recruiter

Senior roles inherit everything below them. Permissions are DATA (the _CAPS
matrix), not scattered `if` checks — add a capability here and every guard that
consults it updates at once.

Capabilities:
  manage_members  add / remove members and send invites
  manage_roles    change a member's role (incl. making someone an admin/manager)
  billing         view and manage the org's credits / billing
  analytics       see org-wide usage ("track user usage")
  settings        edit the organization profile
  use_tools       use the sourcing workspace (everyone in the org)
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import Employer, EmployerMember, User

ORG_ROLES = ("owner", "admin", "manager", "recruiter")
ROLE_LABELS = {"owner": "Owner", "admin": "Admin", "manager": "Manager",
               "recruiter": "Member"}

_RANK = {"owner": 3, "admin": 2, "manager": 1, "recruiter": 0}

# capability -> the roles that hold it (seniors listed explicitly for clarity)
_CAPS: dict[str, set[str]] = {
    "manage_members": {"owner", "admin", "manager"},
    "manage_roles":   {"owner", "admin"},
    "billing":        {"owner", "admin"},
    "analytics":      {"owner", "admin", "manager"},
    "settings":       {"owner", "admin"},
    "use_tools":      {"owner", "admin", "manager", "recruiter"},
}

CAPABILITIES = tuple(_CAPS.keys())


def rank(role: Optional[str]) -> int:
    return _RANK.get(role or "", -1)


def can(role: Optional[str], capability: str) -> bool:
    return (role or "") in _CAPS.get(capability, set())


def is_platform_admin(user: User) -> bool:
    return getattr(user.role, "value", user.role) == "admin"


def role_of(db: DbSession, employer: Employer, user: User) -> Optional[str]:
    """The user's role WITHIN this organization, or None if not a member.

    A platform super-admin is treated as owner-level so the admin console can act
    on any org. The org's actual owner is always 'owner'.
    """
    if employer.owner_user_id == user.user_id or is_platform_admin(user):
        return "owner"
    m = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer.employer_id,
        EmployerMember.user_id == user.user_id))
    return (m.member_role or "recruiter") if m else None


def permissions(role: Optional[str]) -> dict:
    """The full capability map for a role — handed to the frontend so it can show
    only the controls this member is allowed to use."""
    return {cap: can(role, cap) for cap in CAPABILITIES}


def normalize_role(role: str) -> str:
    """Coerce assorted historical labels ('member', 'employer') to a valid role."""
    r = (role or "").strip().lower()
    if r in ("member", "user", "employer", ""):
        return "recruiter"
    return r if r in ORG_ROLES else "recruiter"
