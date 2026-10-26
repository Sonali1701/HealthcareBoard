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
from app.main import app
from app.models import Application, Employer, JobPosting, MatchRun, Profile, User
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

    def tearDown(self):
        self.web_session_patch.stop()
        self.rate_patch.stop()
        app.dependency_overrides.clear()
        self.client.close()
        self.engine.dispose()

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
