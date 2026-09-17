from __future__ import annotations

import unittest
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, utcnow
from app.models import AuditLog, Employer, EmployerMember, TeamInvite, User, UserRole, UserStatus
from app.routers import analytics, employers
from app.routers.extension import MEDHUNT_ATTEMPT_ACTION, MEDHUNT_ENRICHED_ACTION
from app.security import sha256


class TeamInviteAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401

        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.owner = User(
            email="owner@example.com", password_hash="x",
            role=UserRole.recruiter, status=UserStatus.active,
        )
        self.invitee = User(
            email="invitee@example.com", password_hash="x",
            role=UserRole.job_seeker, status=UserStatus.active,
        )
        self.db.add_all([self.owner, self.invitee])
        self.db.flush()
        self.org = Employer(owner_user_id=self.owner.user_id, org_name="Inviting Agency")
        self.db.add(self.org)
        self.db.flush()
        self.db.add(EmployerMember(
            employer_id=self.org.employer_id,
            user_id=self.owner.user_id,
            member_role="owner",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _invite(self, token: str = "valid-invite-token") -> TeamInvite:
        invite = TeamInvite(
            employer_id=self.org.employer_id,
            email=self.invitee.email,
            role="recruiter",
            token_hash=sha256(token),
            status="pending",
            invited_by_user_id=self.owner.user_id,
            expires_at=utcnow() + timedelta(days=1),
        )
        self.db.add(invite)
        self.db.commit()
        return invite

    def test_accept_joins_inviting_org_promotes_user_and_clears_pending(self):
        invite = self._invite()

        result = employers.accept_invite(
            employers.InviteAccept(token="valid-invite-token"), self.invitee, self.db
        )

        membership = self.db.scalar(select(EmployerMember).where(
            EmployerMember.employer_id == self.org.employer_id,
            EmployerMember.user_id == self.invitee.user_id,
        ))
        self.assertIsNotNone(membership)
        self.assertEqual(self.invitee.role, UserRole.recruiter)
        self.assertEqual(invite.status, "accepted")
        self.assertEqual(result["employer"]["employer_id"], self.org.employer_id)
        dashboard = employers.my_employer_dashboard(
            self.invitee, self.db, employer_id=self.org.employer_id
        )
        self.assertEqual(dashboard["employer"]["org_name"], "Inviting Agency")
        pending = employers.list_invites(self.org.employer_id, self.owner, self.db)
        self.assertEqual(pending["items"], [])

    def test_owner_can_see_team_extension_usage_details(self):
        self._invite()
        employers.accept_invite(
            employers.InviteAccept(token="valid-invite-token"), self.invitee, self.db
        )
        self.db.add_all([
            AuditLog(
                actor_user_id=self.owner.user_id,
                action=MEDHUNT_ENRICHED_ACTION,
                entity_type="medhunt_event",
                entity_id="owner-event",
                meta={"candidate_id": "candidate-1", "source": "indeed", "status": "success"},
            ),
            AuditLog(
                actor_user_id=self.invitee.user_id,
                action=MEDHUNT_ATTEMPT_ACTION,
                entity_type="medhunt_event",
                entity_id="member-event",
                meta={"candidate_id": "candidate-2", "source": "vivian", "status": "not_found"},
            ),
        ])
        self.db.commit()

        report = analytics.medhunt_extension_activity(
            self.owner, self.db, days=30, employer_id=self.org.employer_id
        )

        self.assertEqual(report["scope"], "organization")
        self.assertEqual(report["summary"]["users"], 2)
        self.assertEqual(report["summary"]["checks"], 2)
        self.assertEqual(report["summary"]["candidates_enriched"], 1)
        self.assertEqual(report["sources"], [{"source": "indeed", "enriched": 1}])

    def test_invite_cannot_be_claimed_by_a_different_email(self):
        invite = self._invite()
        wrong_user = User(
            email="wrong@example.com", password_hash="x",
            role=UserRole.recruiter, status=UserStatus.active,
        )
        self.db.add(wrong_user)
        self.db.commit()

        with self.assertRaises(HTTPException) as caught:
            employers.accept_invite(
                employers.InviteAccept(token="valid-invite-token"), wrong_user, self.db
            )

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(invite.status, "pending")


if __name__ == "__main__":
    unittest.main()
