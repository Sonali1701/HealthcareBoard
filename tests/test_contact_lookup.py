"""The contact-lookup endpoint, with the external service stubbed out.

Two things here cost real money or real trust if they break:

* **Billing.** A lookup is metered under the same key as a manual reveal, so a
  provider a recruiter already paid for must never be charged again — and a
  search that finds nothing must not be charged at all.
* **The paywall.** A found contact unlocks the provider (it writes the release
  row), because a recruiter who has paid must not still be looking at a masked
  name. A miss must unlock nothing.

The network call itself is replaced with a stub: these tests are about what we
do with an answer, not about the Hub.
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user
from app.main import app as api
from app.models import AuditLog, Profile, User
from app.models.enums import UserRole
from app.services import credits as credit_service
from app.services import quick_sourcer as qs

MATCH = qs.ContactMatch(
    found=True, candidate_id=5, name="Joseph Michael Antario",
    email="joe@example.com", phone="(610) 217-3807",
    address="396 Little Creek Dr, Nazareth, PA 18064", source="familytreenow",
    emails=["joe@example.com"], phones=["(610) 217-3807"],
)


class ContactLookupTests(unittest.TestCase):
    def setUp(self):
        # StaticPool: every session must land on the SAME in-memory database,
        # or the request handler opens a second connection and finds no tables.
        self.engine = create_engine(
            "sqlite:///:memory:", future=True, poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()

        self.user = User(email="recruiter@test.local", password_hash="x",
                         role=UserRole.recruiter)
        self.db.add(self.user)
        self.profile = Profile(first_name="Joseph", last_name="Antario",
                               city="Nazareth", state_code="PA")
        self.db.add(self.profile)
        self.db.commit()
        self.pid = self.profile.profile_id

        api.dependency_overrides[get_db] = lambda: self.Session()
        api.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(api)

        # The endpoint refuses to run at all unless the integration is on.
        self._ready = mock.patch.object(qs, "available", return_value=True)
        self._ready.start()

    def tearDown(self):
        self._ready.stop()
        api.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _lookup(self, match=MATCH):
        async def fake_find(name, location=None):
            self.last_search = (name, location)
            return match

        with mock.patch.object(qs, "find", fake_find):
            return self.client.post(f"/api/profiles/{self.pid}/contact-lookup")

    def _audit(self, action):
        return self.db.scalars(
            select(AuditLog).where(AuditLog.action == action)).all()

    # --- the happy path ---------------------------------------------------

    def test_a_found_contact_is_written_onto_the_profile(self):
        res = self._lookup()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["found"])
        self.assertEqual(body["email"], "joe@example.com")
        self.assertEqual(sorted(body["filled"]), ["email", "phone"])

        self.db.expire_all()
        saved = self.db.get(Profile, self.pid)
        self.assertEqual(saved.email, "joe@example.com")
        self.assertEqual(saved.phone, "(610) 217-3807")
        # Whoever ran the lookup owns the change, same as a manual contact edit.
        self.assertEqual(saved.contact_updated_by_email, self.user.email)

    def test_the_search_uses_the_name_and_the_providers_city(self):
        self._lookup()
        self.assertEqual(self.last_search, ("Joseph Antario", "Nazareth, PA"))

    def test_a_find_unlocks_the_provider(self):
        """Paying for a contact has to lift the mask, or the recruiter has paid
        and is still looking at 'J. A.'."""
        self._lookup()
        self.assertEqual(len(self._audit("provider_contact_released")), 1)
        self.assertTrue(self._lookup().json()["is_released"])

    def test_the_source_and_candidate_id_are_recorded(self):
        self._lookup()
        rows = self._audit("provider_contact_lookup")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].meta["source"], "familytreenow")
        self.assertEqual(rows[0].meta["candidate_id"], 5)

    # --- what must not be overwritten -------------------------------------

    def test_a_contact_we_already_hold_is_never_overwritten(self):
        """A recruiter's own entry, or one off the candidate's résumé, is better
        evidence than a people-search hit. The find is offered as an alternative
        instead."""
        self.profile.email = "known@example.com"
        self.db.commit()

        body = self._lookup().json()
        self.assertEqual(body["filled"], ["phone"])
        self.db.expire_all()
        self.assertEqual(self.db.get(Profile, self.pid).email, "known@example.com")
        self.assertIn("joe@example.com", body["lookup"]["emails"])

    # --- billing ----------------------------------------------------------

    def test_a_miss_is_free_and_unlocks_nothing(self):
        """A miss is regularly the source site throttling us. Charging for our
        own bad luck — and half-unlocking the row — would both be wrong."""
        before = credit_service.balance(self.db, self.user.user_id)
        body = self._lookup(qs.ContactMatch(found=False)).json()
        self.assertFalse(body["found"])
        self.assertEqual(body["credits_charged"], 0)
        self.assertEqual(credit_service.balance(self.db, self.user.user_id), before)
        self.assertEqual(self._audit("provider_contact_released"), [])

    @unittest.skipUnless(settings.credits_enabled, "credits are switched off")
    def test_the_same_provider_is_only_ever_charged_once(self):
        first = self._lookup().json()
        self.assertEqual(first["credits_charged"], 1)
        # Wipe what we saved so the second call takes the same path again.
        self.profile.email = self.profile.phone = None
        self.db.commit()
        self.assertEqual(self._lookup().json()["credits_charged"], 0)

    @unittest.skipUnless(settings.credits_enabled, "credits are switched off")
    def test_an_empty_balance_stops_the_lookup_being_saved(self):
        credit_service.get_account(self.db, self.user.user_id)
        self.db.execute(
            AuditLog.__table__.delete())          # no releases to inherit
        self.db.execute(
            text("UPDATE credit_accounts SET balance = 0 WHERE user_id = :u"),
            {"u": self.user.user_id})
        self.db.commit()

        res = self._lookup()
        self.assertEqual(res.status_code, 402)
        self.db.expire_all()
        self.assertIsNone(self.db.get(Profile, self.pid).email)

    # --- refusals ---------------------------------------------------------

    def test_a_provider_without_a_full_name_is_not_searched(self):
        """One token matches half a state; searching on it burns 90 seconds to
        return the wrong person."""
        self.profile.last_name = ""
        self.db.commit()
        res = self._lookup()
        self.assertEqual(res.status_code, 400)

    def test_the_endpoint_is_off_when_the_integration_is_not_configured(self):
        with mock.patch.object(qs, "available", return_value=False):
            res = self.client.post(f"/api/profiles/{self.pid}/contact-lookup")
        self.assertEqual(res.status_code, 503)

    def test_a_job_seeker_cannot_mine_the_directory(self):
        self.user.role = UserRole.job_seeker
        res = self._lookup()
        self.assertIn(res.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
