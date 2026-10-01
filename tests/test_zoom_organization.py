from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, utcnow
from app.models import (
    Employer, EmployerMember, MedhuntMessagingPermission, MedhuntSmsSender,
    User, UserRole, UserStatus, ZoomOrganizationIntegration,
)
from app.services import zoom_oauth


class ZoomOrganizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.owner = User(email="owner@example.com", password_hash="x",
                          role=UserRole.recruiter, status=UserStatus.active)
        self.member = User(email="member@example.com", password_hash="x",
                           role=UserRole.recruiter, status=UserStatus.active)
        self.db.add_all([self.owner, self.member])
        self.db.flush()
        self.org = Employer(owner_user_id=self.owner.user_id, org_name="Example Staffing")
        self.db.add(self.org)
        self.db.flush()
        self.db.add_all([
            EmployerMember(employer_id=self.org.employer_id, user_id=self.owner.user_id,
                           member_role="owner"),
            EmployerMember(employer_id=self.org.employer_id, user_id=self.member.user_id),
        ])
        self.integration = ZoomOrganizationIntegration(
            employer_id=self.org.employer_id, zoom_account_id="zoom-account",
            access_token_encrypted=zoom_oauth.encrypt("access-token"),
            refresh_token_encrypted=zoom_oauth.encrypt("refresh-token"),
            access_token_expires_at=utcnow() + timedelta(hours=1),
            connected_by_user_id=self.owner.user_id,
        )
        self.db.add(self.integration)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.services.zoom_oauth._zoom_get")
    def test_sync_matches_by_email_and_requires_explicit_approval(self, zoom_get):
        zoom_get.return_value = {"users": [{
            "id": "zoom-user-member", "email": "MEMBER@example.com",
            "phone_numbers": [{"number": "+1 (415) 555-0123"}],
        }]}
        result = zoom_oauth.sync_members(
            self.db, self.org, self.integration, self.owner,
        )
        self.assertEqual(result["matched_members"], 1)
        self.assertEqual(result["configured_senders"], 1)
        sender = self.db.scalar(select(MedhuntSmsSender).where(
            MedhuntSmsSender.user_id == self.member.user_id,
        ))
        self.assertEqual(sender.sender_number, "+14155550123")
        self.assertEqual(sender.zoom_user_id, "zoom-user-member")
        permission = self.db.scalar(select(MedhuntMessagingPermission).where(
            MedhuntMessagingPermission.user_id == self.member.user_id,
        ))
        self.assertEqual(permission.status, "disabled")

    def test_tokens_are_encrypted_at_rest(self):
        self.assertNotIn("access-token", self.integration.access_token_encrypted)
        self.assertEqual(zoom_oauth.access_token(self.db, self.integration), "access-token")


if __name__ == "__main__":
    unittest.main()
