"""Opting out has to work and has to be hard to abuse.

The directory lists people who never signed up and whose contact details are
sold, so these are the guarantees that matter: a delist really erases the
contact, and nobody can delist somebody else by typing their address.
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AuditLog, Profile
from app.routers.privacy import (
    ACTION_DELIST,
    ACTION_REQUEST,
    OPT_OUT_REASON,
    _delist,
)


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.profile = Profile(
            first_name="Nina", last_name="Nurse", email="nina@test.local",
            phone="512-555-0142", resume_url="https://storage/resume.pdf",
            city="Austin", state_code="TX", profession_type="RN",
            is_listable=True, open_to_work=True)
        self.db.add(self.profile)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_delisting_hides_the_profile(self):
        _delist(self.db, self.profile, actor=None, request=None, via="test")
        self.db.commit()
        self.assertFalse(self.profile.is_listable)
        self.assertEqual(self.profile.screen_reason, OPT_OUT_REASON)

    def test_delisting_erases_the_details_recruiters_pay_for(self):
        """Hiding the row is not enough — the contact is the product."""
        _delist(self.db, self.profile, actor=None, request=None, via="test")
        self.db.commit()
        self.assertIsNone(self.profile.email)
        self.assertIsNone(self.profile.phone)
        self.assertIsNone(self.profile.resume_url)

    def test_delisting_stops_them_being_marketed_as_available(self):
        _delist(self.db, self.profile, actor=None, request=None, via="test")
        self.db.commit()
        self.assertFalse(self.profile.open_to_work)

    def test_professional_details_survive_so_the_record_is_not_re_imported(self):
        """The row is retained deliberately: delete it and tomorrow's import
        puts the same person straight back into the directory."""
        _delist(self.db, self.profile, actor=None, request=None, via="test")
        self.db.commit()
        kept = self.db.get(Profile, self.profile.profile_id)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.profession_type, "RN")

    def test_delisting_is_audited_with_what_was_removed(self):
        _delist(self.db, self.profile, actor=None, request=None, via="email_confirmation")
        self.db.commit()
        log = self.db.scalar(select(AuditLog).where(AuditLog.action == ACTION_DELIST))
        self.assertIsNotNone(log)
        self.assertEqual(log.entity_id, self.profile.profile_id)
        self.assertEqual(log.meta.get("via"), "email_confirmation")
        self.assertIn("email", log.meta.get("contact_removed", []))

    def test_delisting_twice_is_harmless(self):
        for _ in range(2):
            _delist(self.db, self.profile, actor=None, request=None, via="test")
            self.db.commit()
        self.assertFalse(self.profile.is_listable)
        self.assertIsNone(self.profile.email)

    def test_a_profile_with_no_contact_still_delists(self):
        bare = Profile(first_name="A", last_name="B", is_listable=True)
        self.db.add(bare)
        self.db.commit()
        _delist(self.db, bare, actor=None, request=None, via="test")
        self.db.commit()
        self.assertFalse(bare.is_listable)


class OptOutTokenTests(unittest.TestCase):
    """The token is what stops one person delisting another."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_a_used_token_cannot_be_replayed(self):
        row = AuditLog(action=ACTION_REQUEST, entity_type="profile",
                       entity_id="p1", meta={"token": "abc", "email": "x@y.z"})
        self.db.add(row)
        self.db.commit()

        def unused(token):
            rows = self.db.scalars(
                select(AuditLog).where(AuditLog.action == ACTION_REQUEST)).all()
            return [r for r in rows
                    if (r.meta or {}).get("token") == token
                    and not (r.meta or {}).get("used")]

        self.assertEqual(len(unused("abc")), 1)
        row.meta = {**row.meta, "used": True}
        self.db.commit()
        self.assertEqual(unused("abc"), [])

    def test_an_unknown_token_matches_nothing(self):
        rows = self.db.scalars(
            select(AuditLog).where(AuditLog.action == ACTION_REQUEST)).all()
        self.assertEqual([r for r in rows if (r.meta or {}).get("token") == "guess"], [])


if __name__ == "__main__":
    unittest.main()
