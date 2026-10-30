"""Launch checks for mounted web routes and email-verification access."""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from app.config import settings
from app.database import Base, get_db
from app.main import _public_stats_uncached, app
from app.models import Application, CreditAccount, Employer, JobPosting, MatchRun, Profile, User
from app.routers.admin import _overview_profile_counts
from app.routers.profiles import _copilot_rule_filters


class LaunchWebFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        app.dependency_overrides[get_db] = lambda: self.sessions()
        self.web_session_patch = mock.patch("app.main.SessionLocal", self.sessions)
        self.web_session_patch.start()
        self.client = TestClient(app, follow_redirects=False)
        self.rate_patch = mock.patch.object(settings, "rate_limit_enabled", False)
        self.rate_patch.start()
        self.email_key_patch = mock.patch.object(settings, "sendgrid_api_key", "test-only-key")
        self.email_key_patch.start()

    def tearDown(self):
        self.email_key_patch.stop()
        self.web_session_patch.stop()
        self.rate_patch.stop()
        app.dependency_overrides.clear()
        self.client.close()
        self.engine.dispose()

    def test_admin_profile_count_uses_fast_estimates_on_postgres(self):
        db = mock.Mock()
        db.get_bind.return_value.dialect.name = "postgresql"
        db.execute.return_value.all.return_value = [
            ("profiles", 12000000.0), ("ix_profiles_listable_cat", 11600000.0),
        ]
        self.assertEqual(_overview_profile_counts(db), (12000000, 11600000, True))
        db.scalar.assert_not_called()

    def test_admin_profile_count_remains_exact_on_sqlite(self):
        with self.sessions() as db:
            self.assertEqual(_overview_profile_counts(db), (0, 0, False))

    def test_public_stats_are_exact_on_sqlite(self):
        with self.sessions() as db:
            db.add_all([
                Profile(first_name="One", last_name="Nurse", state_code="TX",
                        is_listable=True),
                Profile(first_name="Two", last_name="Nurse", state_code="TX",
                        is_listable=True),
                Profile(first_name="Hidden", last_name="Nurse", state_code="CA",
                        is_listable=False),
            ])
            owner = User(email="stats-owner@example.com", password_hash="unused",
                         role="recruiter", status="active")
            db.add(owner)
            db.flush()
            employer = Employer(owner_user_id=owner.user_id, org_name="Stats Clinic")
            db.add(employer)
            db.flush()
            db.add(JobPosting(employer_id=employer.employer_id,
                              title="Open role", status="active"))
            db.commit()
            self.assertEqual(_public_stats_uncached(db), {
                "providers": 2, "jobs": 1, "states": 1,
            })

    def test_public_stats_use_profile_index_estimate_on_postgres(self):
        db = mock.Mock()
        db.get_bind.return_value.dialect.name = "postgresql"
        results = [mock.Mock(), mock.Mock(), mock.Mock()]
        results[0].scalar.return_value = 11600000
        results[1].scalar.return_value = 51
        results[2].scalar.return_value = 501
        db.execute.side_effect = results
        self.assertEqual(_public_stats_uncached(db), {
            "providers": 11600000, "jobs": 501, "states": 51,
        })
        self.assertEqual(db.execute.call_count, 3)

    def test_web_signup_login_and_local_redirect(self):
        page = self.client.get("/app/signup")
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/app/signup"', page.text)
        with mock.patch.object(settings, "email_enabled", False):
            created = self.client.post("/app/signup", data={
                "email": "web-seeker@example.com", "password": "LocalPass123!",
                "role": "job_seeker", "first_name": "Web", "last_name": "Seeker",
            })
        self.assertEqual(created.status_code, 303)
        self.assertEqual(created.headers["location"], "/app/dashboard")
        logged_in = self.client.post("/app/login", data={
            "email": "web-seeker@example.com", "password": "LocalPass123!",
            "next": "//outside.example",
        })
        self.assertEqual(logged_in.status_code, 303)
        self.assertEqual(logged_in.headers["location"], "/app/dashboard")

    def test_web_signup_rejects_invalid_email(self):
        created = self.client.post("/app/signup", data={
            "email": "not-an-email", "password": "LocalPass123!",
            "role": "job_seeker",
        })
        self.assertEqual(created.status_code, 400)
        self.assertIn("Enter a valid email address", created.text)
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 0)

    def test_missing_mail_key_does_not_create_unverifiable_accounts(self):
        with (mock.patch.object(settings, "email_enabled", True),
              mock.patch.object(settings, "sendgrid_api_key", "")):
            api = self.client.post("/api/auth/register", json={
                "email": "no-mail-api@example.com", "password": "LocalPass123!",
                "role": "recruiter",
            })
            web = self.client.post("/app/signup", data={
                "email": "no-mail-web@example.com", "password": "LocalPass123!",
                "role": "job_seeker",
            })
        self.assertEqual(api.status_code, 503)
        self.assertEqual(web.status_code, 503)
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 0)

    def test_public_job_links_and_auth_redirect_stay_under_app(self):
        jobs = self.client.get("/app/jobs")
        self.assertEqual(jobs.status_code, 200)
        self.assertNotRegex(jobs.text, r'(?:href|action)="/(?:jobs|login|signup)(?:[/?"]|$)')
        dashboard = self.client.get("/app/dashboard")
        self.assertEqual(dashboard.status_code, 303)
        self.assertTrue(dashboard.headers["location"].startswith("/app/login?next="))

    def test_pending_api_account_cannot_search_until_verified(self):
        sent = []
        with (mock.patch.object(settings, "email_enabled", True),
              mock.patch("app.routers.auth.send_email_verification",
                         side_effect=lambda email, token: sent.append(token) or True)):
            created = self.client.post("/api/auth/register", json={
                "email": "pending-recruiter@example.com", "password": "LocalPass123!",
                "role": "recruiter",
            })
        self.assertEqual(created.status_code, 201, created.text)
        headers = {"Authorization": "Bearer " + created.json()["access_token"]}
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).json()["status"],
                         "pending_verify")
        denied = self.client.get("/api/profiles?providers_only=true", headers=headers)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.post(
            "/api/auth/email/request-verification", headers=headers).status_code, 200)
        self.assertEqual(self.client.post(
            "/api/auth/email/verify", params={"token": sent[0]}).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/profiles?providers_only=true", headers=headers).status_code, 200)

    def test_web_signup_respects_email_verification(self):
        sent = []
        with (mock.patch.object(settings, "email_enabled", True),
              mock.patch("app.web.routes.auth.send_email_verification",
                         side_effect=lambda email, token: sent.append(token) or True)):
            created = self.client.post("/app/signup", data={
                "email": "web-pending@example.com", "password": "LocalPass123!",
                "role": "recruiter", "first_name": "Web", "last_name": "Pending",
            })
        self.assertEqual(created.status_code, 200)
        self.assertIn("Check your email", created.text)
        login = self.client.post("/app/login", data={
            "email": "web-pending@example.com", "password": "LocalPass123!",
        })
        self.assertEqual(login.status_code, 403)
        self.assertEqual(self.client.post(
            "/api/auth/email/verify", params={"token": sent[0]}).status_code, 200)
        login = self.client.post("/app/login", data={
            "email": "web-pending@example.com", "password": "LocalPass123!",
        })
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/app/recruiter")

    def test_applicant_cannot_apply_with_someone_elses_profile(self):
        with mock.patch.object(settings, "email_enabled", False):
            first = self.client.post("/api/auth/register", json={
                "email": "applicant-one@example.com", "password": "LocalPass123!",
                "role": "job_seeker", "first_name": "One", "last_name": "Applicant",
            })
            second = self.client.post("/api/auth/register", json={
                "email": "applicant-two@example.com", "password": "LocalPass123!",
                "role": "job_seeker", "first_name": "Two", "last_name": "Applicant",
            })
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        with self.sessions() as db:
            first_user = db.scalar(select(User).where(User.email == "applicant-one@example.com"))
            second_user = db.scalar(select(User).where(User.email == "applicant-two@example.com"))
            first_profile = db.scalar(select(Profile).where(Profile.user_id == first_user.user_id))
            second_profile = db.scalar(select(Profile).where(Profile.user_id == second_user.user_id))
            employer = Employer(owner_user_id=first_user.user_id, org_name="Audit Clinic")
            db.add(employer)
            db.flush()
            job = JobPosting(employer_id=employer.employer_id, title="Audit RN")
            db.add(job)
            db.commit()
            job_id, first_id, second_id = job.job_id, first_profile.profile_id, second_profile.profile_id
        headers = {"Authorization": "Bearer " + first.json()["access_token"]}
        denied = self.client.post(f"/api/jobs/{job_id}/apply", headers=headers,
                                  json={"profile_id": second_id})
        self.assertEqual(denied.status_code, 403, denied.text)
        with self.sessions() as db:
            self.assertEqual(db.query(Application).count(), 0)
        allowed = self.client.post(f"/api/jobs/{job_id}/apply", headers=headers,
                                   json={"profile_id": first_id})
        self.assertEqual(allowed.status_code, 201, allowed.text)

    def test_copilot_without_model_keeps_structured_queries_clean(self):
        with mock.patch.object(settings, "email_enabled", False):
            created = self.client.post("/api/auth/register", json={
                "email": "search-recruiter@example.com", "password": "LocalPass123!",
                "role": "recruiter",
            })
        self.assertEqual(created.status_code, 201, created.text)
        with self.sessions() as db:
            nurse = Profile(first_name="Jane", last_name="Nurse",
                            specialty="Registered Nurse", profession_type="RN",
                            job_category="Nursing", state_code="TX")
            nurse.rebuild_search_text()
            db.add(nurse)
            db.commit()
        headers = {"Authorization": "Bearer " + created.json()["access_token"]}
        with mock.patch.object(settings, "llm_enabled", False):
            result = self.client.post("/api/profiles/copilot", headers=headers,
                                      json={"message": "registered nurse in Texas"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["total"], 1)
        self.assertEqual(result.json()["filters"].get("q"), None)
        facets = self.client.get("/api/profiles/facets?basic=true", headers=headers)
        self.assertEqual(facets.status_code, 200, facets.text)
        self.assertEqual(facets.json()["categories"]["Nursing"], 1)
        self.assertEqual(facets.json()["states"], ["TX"])
        self.assertNotIn("cities", facets.json())

    def test_analytics_aggregates_match_runs(self):
        with mock.patch.object(settings, "email_enabled", False):
            created = self.client.post("/api/auth/register", json={
                "email": "analytics-recruiter@example.com", "password": "LocalPass123!",
                "role": "recruiter",
            })
        self.assertEqual(created.status_code, 201, created.text)
        with self.sessions() as db:
            user = db.scalar(select(User).where(User.email == "analytics-recruiter@example.com"))
            db.add_all([
                MatchRun(requested_by_user_id=user.user_id, candidate_count=4, avg_score=80),
                MatchRun(requested_by_user_id=user.user_id, candidate_count=6, avg_score=60),
            ])
            db.commit()
        headers = {"Authorization": "Bearer " + created.json()["access_token"]}
        result = self.client.get("/api/analytics/sourcing", headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["sourcing_runs"], {
            "runs": 2, "candidates_ranked": 10, "avg_match_score": 70.0,
        })
        lean = self.client.get("/api/analytics/sourcing?include_directory=false",
                               headers=headers)
        self.assertEqual(lean.status_code, 200, lean.text)
        self.assertIsNone(lean.json()["directory"])
        self.assertEqual(lean.json()["sourcing_runs"], result.json()["sourcing_runs"])

    def test_market_cache_keeps_credit_balances_per_user(self):
        tokens = []
        with mock.patch.object(settings, "email_enabled", False):
            for number in (1, 2):
                created = self.client.post("/api/auth/register", json={
                    "email": f"market-{number}@example.com", "password": "LocalPass123!",
                    "role": "recruiter",
                })
                self.assertEqual(created.status_code, 201, created.text)
                tokens.append(created.json()["access_token"])
        with self.sessions() as db:
            users = [db.scalar(select(User).where(User.email == f"market-{n}@example.com"))
                     for n in (1, 2)]
            db.add_all([CreditAccount(user_id=users[0].user_id, balance=3),
                        CreditAccount(user_id=users[1].user_id, balance=7)])
            db.commit()
        first = self.client.get("/api/analytics/market", headers={
            "Authorization": "Bearer " + tokens[0]})
        self.assertEqual(first.status_code, 200, first.text)
        with mock.patch("app.routers.analytics._market_uncached",
                        side_effect=AssertionError("shared market data was recomputed")):
            second = self.client.get("/api/analytics/market", headers={
                "Authorization": "Bearer " + tokens[1]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["credits"]["balance"], 3)
        self.assertEqual(second.json()["credits"]["balance"], 7)


class CopilotFallbackTests(unittest.TestCase):
    def test_location_and_correction_edge_cases(self):
        self.assertEqual(_copilot_rule_filters("RN ICU within 25 miles of 90210"), {
            "radius_mi": 25, "zip": "90210", "license_title": "RN",
            "category": "Nursing", "q": "icu",
        })
        self.assertEqual(_copilot_rule_filters("not RN, I meant LPN"), {
            "license_title": "LPN", "category": "Nursing",
        })


if __name__ == "__main__":
    unittest.main()
