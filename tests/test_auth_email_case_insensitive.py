from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import PasswordResetToken, User, UserRole, UserStatus
from app.routers.auth import _login, password_reset_request
from app.schemas.auth import LoginRequest, PasswordResetRequest
from app.security import hash_password


class AuthEmailCaseInsensitiveTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401

        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = User(
            email="Tyler@radixsol.com",
            password_hash=hash_password("correct-horse-battery"),
            role=UserRole.recruiter,
            status=UserStatus.active,
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_login_matches_email_without_case_sensitivity(self):
        tokens = _login(
            LoginRequest(email="tyler@RADIXSOL.com", password="correct-horse-battery"),
            self.db,
            request=None,
        )

        self.assertTrue(tokens.access_token)
        self.assertTrue(tokens.refresh_token)
        self.assertIsNotNone(self.user.last_login_at)

    def test_password_reset_matches_email_without_case_sensitivity(self):
        with patch("app.routers.auth.send_password_reset", return_value=True) as send:
            result = password_reset_request(
                PasswordResetRequest(email="TYLER@RADIXSOL.COM"),
                self.db, request=None, _rl=None,
            )

        self.assertEqual(result["detail"], "If the email exists, a reset link was sent")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], self.user.email)
        reset = self.db.scalar(select(PasswordResetToken).where(
            PasswordResetToken.user_id == self.user.user_id
        ))
        self.assertIsNotNone(reset)


if __name__ == "__main__":
    unittest.main()
