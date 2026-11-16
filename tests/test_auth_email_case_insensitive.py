from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import User, UserRole, UserStatus
from app.routers.auth import _login
from app.schemas.auth import LoginRequest
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


if __name__ == "__main__":
    unittest.main()
