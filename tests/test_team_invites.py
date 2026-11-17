from __future__ import annotations

import unittest
from unittest.mock import patch
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, utcnow
from app.models import (
    AuditLog, ContactLookupJob, Employer, EmployerMember, OrganizationTeam,
    OrganizationTeamMember, Profile, ProviderDirectoryContactLookupJob,
    TeamInvite, User, UserRole, UserStatus,
)
from app.routers import analytics, employers, profiles
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
        team_membership = self.db.scalar(select(OrganizationTeamMember).where(
            OrganizationTeamMember.user_id == self.invitee.user_id,
        ))
        self.assertIsNotNone(team_membership)
        team = self.db.get(OrganizationTeam, team_membership.team_id)
        self.assertEqual(team.name, "General")
        dashboard = employers.my_employer_dashboard(
            self.invitee, self.db, employer_id=self.org.employer_id
        )
        self.assertEqual(dashboard["employer"]["org_name"], "Inviting Agency")
        pending = employers.list_invites(self.org.employer_id, self.owner, self.db)
        self.assertEqual(pending["items"], [])

    def test_pending_invite_is_reconciled_when_user_is_already_a_member(self):
        invite = self._invite()
        # Existing-user invitations can add a member independently of the
        # token acceptance flow, leaving a stale pending invitation behind.
        self.db.add(EmployerMember(
            employer_id=self.org.employer_id,
            user_id=self.invitee.user_id,
            member_role="recruiter",
        ))
        invite.email = self.invitee.email.upper()
        self.db.commit()

        pending = employers.list_invites(self.org.employer_id, self.owner, self.db)

        self.assertEqual(pending["items"], [])
        self.assertEqual(invite.status, "accepted")

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
        self.assertEqual({row["source"]: (row["checks"], row["enriched"])
                          for row in report["sources"]},
                         {"indeed": (1, 1), "vivian": (1, 0)})
        self.assertEqual(report["daily"][0]["checks"], 2)
        self.assertEqual(report["daily"][0]["users"], 2)
        self.assertEqual(report["daily"][0]["candidates_enriched"], 1)
        self.assertEqual(report["activity_total"], 2)
        self.assertEqual(len(report["member_options"]), 2)

        filtered = analytics.medhunt_extension_activity(
            self.owner, self.db, days=30, employer_id=self.org.employer_id,
            member_id=self.owner.user_id, source="INDEED", outcome="enriched",
            page=1, page_size=1,
        )
        self.assertEqual(filtered["summary"]["checks"], 1)
        self.assertEqual(filtered["summary"]["enrichments"], 1)
        self.assertEqual(filtered["activity_total"], 1)
        self.assertEqual(filtered["activity"][0]["user_email"], self.owner.email)
        self.assertEqual(filtered["activity"][0]["event_id"], "owner-event")
        self.assertEqual(len(filtered["source_options"]), 2)

        today = utcnow().date()
        by_platform_and_date = analytics.medhunt_extension_activity(
            self.owner, self.db, days=30, employer_id=self.org.employer_id,
            platform="indeed", date_from=today, date_to=today,
        )
        self.assertEqual(by_platform_and_date["summary"]["checks"], 1)
        self.assertEqual(by_platform_and_date["activity"][0]["platform"], "indeed")
        self.assertEqual(by_platform_and_date["filters"]["date_from"], today.isoformat())
        self.assertEqual(by_platform_and_date["platform_options"], ["indeed", "vivian"])

        with self.assertRaises(HTTPException) as invalid_range:
            analytics.medhunt_extension_activity(
                self.owner, self.db, days=30, employer_id=self.org.employer_id,
                date_from=today, date_to=today - timedelta(days=1),
            )
        self.assertEqual(invalid_range.exception.status_code, 422)

        second_page = analytics.medhunt_extension_activity(
            self.owner, self.db, days=30, employer_id=self.org.employer_id,
            page=2, page_size=1,
        )
        self.assertEqual(second_page["activity_total"], 2)
        self.assertEqual(len(second_page["activity"]), 1)

        with self.assertRaises(HTTPException) as denied:
            analytics.medhunt_extension_activity(
                self.invitee, self.db, days=30, employer_id=self.org.employer_id,
                member_id=self.owner.user_id,
            )
        self.assertEqual(denied.exception.status_code, 403)

    def test_website_lookup_report_is_organization_scoped_and_owner_admin_only(self):
        admin = User(email="admin@example.com", password_hash="x",
                     role=UserRole.recruiter, status=UserStatus.active)
        other = User(email="other@example.com", password_hash="x",
                     role=UserRole.recruiter, status=UserStatus.active)
        candidate = Profile(first_name="Alex", last_name="Rivera")
        self.db.add_all([admin, other, candidate])
        self.db.flush()
        other_org = Employer(owner_user_id=other.user_id, org_name="Other Agency")
        self.db.add(other_org)
        self.db.flush()
        self.db.add_all([
            EmployerMember(employer_id=self.org.employer_id,
                           user_id=admin.user_id, member_role="admin"),
            EmployerMember(employer_id=self.org.employer_id,
                           user_id=self.invitee.user_id, member_role="recruiter"),
            ContactLookupJob(requester_user_id=self.owner.user_id,
                             employer_id=self.org.employer_id,
                             profile_id=candidate.profile_id, status="completed",
                             found=True, source="quick_sourcer", credits_charged=1),
            ContactLookupJob(requester_user_id=self.invitee.user_id,
                             employer_id=self.org.employer_id,
                             profile_id=candidate.profile_id, status="failed",
                             detail="Timed out"),
            ProviderDirectoryContactLookupJob(
                requester_user_id=self.invitee.user_id,
                employer_id=self.org.employer_id, npi="1234567890",
                lookup_name="Jordan Lee", status="completed", found=True,
                source="quick_sourcer", credits_charged=1),
            ContactLookupJob(requester_user_id=other.user_id,
                             employer_id=other_org.employer_id,
                             profile_id=candidate.profile_id, status="completed", found=True),
            ContactLookupJob(requester_user_id=self.owner.user_id,
                             profile_id=candidate.profile_id, status="not_found"),
        ])
        self.db.commit()

        report = analytics.website_contact_lookups(
            self.owner, self.db, self.org.employer_id)
        self.assertEqual(report["summary"]["lookups"], 3)
        self.assertEqual(report["summary"]["found"], 2)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["credits_charged"], 2)
        self.assertEqual({row["email"] for row in report["members"]},
                         {self.owner.email, self.invitee.email})
        self.assertEqual(report["daily"][0]["lookups"], 3)
        self.assertEqual(report["activity_total"], 3)
        self.assertEqual(len(report["activity"]), 3)
        self.assertEqual({item["candidate_name"] for item in report["activity"]},
                         {"Alex Rivera", "Jordan Lee"})
        self.assertEqual(next(item for item in report["activity"] if item["npi"])["directory"],
                         "NPI directory")

        admin_report = analytics.website_contact_lookups(
            admin, self.db, self.org.employer_id, outcome="failed",
            member_id=self.invitee.user_id, page_size=1)
        self.assertEqual(admin_report["summary"]["lookups"], 1)
        self.assertEqual(admin_report["activity"][0]["detail"], "Timed out")
        today = utcnow().date()
        dated = analytics.website_contact_lookups(
            self.owner, self.db, self.org.employer_id,
            date_from=today, date_to=today)
        self.assertEqual(dated["summary"]["lookups"], 3)
        with self.assertRaises(HTTPException) as denied:
            analytics.website_contact_lookups(self.invitee, self.db, self.org.employer_id)
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as cross_org:
            analytics.website_contact_lookups(self.owner, self.db, other_org.employer_id)
        self.assertEqual(cross_org.exception.status_code, 403)
        with self.assertRaises(HTTPException) as cross_member:
            analytics.website_contact_lookups(
                self.owner, self.db, self.org.employer_id, member_id=other.user_id)
        self.assertEqual(cross_member.exception.status_code, 403)

    def test_website_lookup_queue_records_organization_and_checks_membership(self):
        candidate = Profile(first_name="Sam", last_name="Taylor")
        self.db.add(candidate)
        self.db.commit()
        with patch.object(profiles.quick_sourcer, "available", return_value=True):
            jobs = profiles._enqueue_contact_lookups(
                self.db, self.owner, [candidate.profile_id], self.org.employer_id)
            self.assertEqual(jobs[0].employer_id, self.org.employer_id)
            with self.assertRaises(HTTPException) as denied:
                profiles._enqueue_contact_lookups(
                    self.db, self.invitee, [candidate.profile_id], self.org.employer_id)
            self.assertEqual(denied.exception.status_code, 403)

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
