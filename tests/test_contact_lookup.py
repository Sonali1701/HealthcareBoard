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

import asyncio
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
from app.services import contact_lookup_queue as lookup_queue
from app.services import quick_sourcer as qs
from app.routers.profiles import _profile_card

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
                               city="Nazareth", state_code="PA", npi_number="1234567890",
                               resume_url="uploads/joseph.pdf",
                               resume_sections={"sections": {"Professional Summary": ["Clinician"]}})
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

        async def fake_fetch(candidate_id):
            return match

        with (mock.patch.object(qs, "find", fake_find),
              mock.patch.object(qs, "fetch_candidate", fake_fetch),
              mock.patch.object(lookup_queue, "SessionLocal", self.Session)):
            queued = self.client.post(f"/api/profiles/{self.pid}/contact-lookup")
            if queued.status_code != 200:
                return queued
            job_id = queued.json()["job_id"]
            self.assertEqual(lookup_queue._claim_one(), job_id)
            asyncio.run(lookup_queue._run_one(job_id))
            return self.client.get(f"/api/profiles/contact-lookup-queue/{job_id}")

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
        self.assertEqual(body["status"], "completed")

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
        self.assertTrue(body["found"])
        self.db.expire_all()
        self.assertEqual(self.db.get(Profile, self.pid).email, "known@example.com")
        self.assertEqual(body["email"], "known@example.com")

    def test_bulk_reveal_charges_once_for_each_new_candidate(self):
        second = Profile(first_name="Taylor", last_name="Morgan",
                         city="Austin", state_code="TX", npi_number="0987654321")
        self.db.add(second)
        credit_service.get_account(self.db, self.user.user_id)
        self.db.commit()
        before = credit_service.balance(self.db, self.user.user_id)

        response = self.client.post("/api/profiles/contact-release/bulk", json={
            "profile_ids": [self.pid, second.profile_id],
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["newly_released"], 2)
        self.assertEqual(body["credits_charged"], 2)
        self.assertEqual(credit_service.balance(self.db, self.user.user_id), before - 2)
        self.assertEqual({item["npi_number"] for item in body["items"]},
                         {"1234567890", "0987654321"})

        repeated = self.client.post("/api/profiles/contact-release/bulk", json={
            "profile_ids": [self.pid, second.profile_id],
        }).json()
        self.assertEqual(repeated["newly_released"], 0)
        self.assertEqual(repeated["credits_charged"], 0)
        self.assertEqual(credit_service.balance(self.db, self.user.user_id), before - 2)
    def test_resume_shows_npi_before_contact_is_revealed(self):
        visible = self.client.get(f"/api/profiles/{self.pid}/resume")
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual(visible.json()["npi_number"], "1234567890")

        revealed = self.client.post(f"/api/profiles/{self.pid}/contact-release")
        self.assertEqual(revealed.status_code, 200, revealed.text)
        still_visible = self.client.get(f"/api/profiles/{self.pid}/resume")
        self.assertEqual(still_visible.json()["npi_number"], "1234567890")
    def test_misfiled_npi_is_shown_at_top_not_as_work_authorization(self):
        self.profile.npi_number = None
        self.profile.work_authorization = "NPI: 1043204035"
        self.db.commit()

        resume = self.client.get(f"/api/profiles/{self.pid}/resume").json()
        self.assertEqual(resume["npi_number"], "1043204035")
        self.assertIsNone(resume["work_authorization"])
        self.assertEqual(
            _profile_card(self.profile, released=False)["npi_number"],
            "1043204035",
        )
    def test_resume_removes_placeholder_only_education(self):
        self.profile.education = [
            {"institution": "—", "degree": "—", "field": None, "year": 1994},
            {"institution": "State University", "degree": "BSN", "year": 2001},
        ]
        self.db.commit()

        education = self.client.get(f"/api/profiles/{self.pid}/resume").json()["education"]
        self.assertEqual(education, [{
            "institution": "State University", "degree": "BSN",
            "field": None, "year": 2001,
        }])

    @unittest.skipUnless(settings.credits_enabled, "credits are switched off")

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
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "failed")
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

    def test_bulk_request_is_queued_and_returns_without_waiting_for_lookup(self):
        second = Profile(first_name="Maya", last_name="Patel", city="Dallas", state_code="TX")
        self.db.add(second)
        self.db.commit()
        res = self.client.post("/api/profiles/contact-lookup-queue",
                               json={"profile_ids": [self.pid, second.profile_id]})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(len(body["jobs"]), 2)
        self.assertEqual({j["status"] for j in body["jobs"]}, {"queued"})

    def test_queue_status_is_private_to_the_requesting_recruiter(self):
        queued = self.client.post("/api/profiles/contact-lookup-queue",
                                  json={"profile_ids": [self.pid]}).json()["jobs"][0]
        other = User(email="other@test.local", password_hash="x", role=UserRole.recruiter)
        self.db.add(other)
        self.db.commit()
        api.dependency_overrides[get_current_user] = lambda: other
        res = self.client.get(f"/api/profiles/contact-lookup-queue/{queued['job_id']}")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
