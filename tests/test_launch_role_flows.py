"""End-to-end API checks for the recruiter and platform-admin launch paths."""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db, utcnow
from app.main import app
from app.models import Employer, Profile, User
from app.models.enums import ProfileSource, UserRole, UserStatus
from app.security import create_access_token, hash_password


class LaunchRoleFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        app.dependency_overrides[get_db] = lambda: self.sessions()
        self.client = TestClient(app)
        self.patches = [
            mock.patch.object(settings, "rate_limit_enabled", False),
            mock.patch.object(settings, "email_enabled", False),
            mock.patch.object(settings, "credits_enabled", True),
        ]
        for patcher in self.patches:
            patcher.start()

        with self.sessions() as db:
            self.recruiter = User(
                email="launch-recruiter@example.test", password_hash=hash_password("TestPass123!"),
                role=UserRole.recruiter, status=UserStatus.active,
                email_verified_at=utcnow(),
            )
            self.outsider = User(
                email="launch-outsider@example.test", password_hash="x",
                role=UserRole.recruiter, status=UserStatus.active,
                email_verified_at=utcnow(),
            )
            self.admin = User(
                email="launch-admin@example.test", password_hash="x",
                role=UserRole.admin, status=UserStatus.active,
                email_verified_at=utcnow(),
            )
            db.add_all([self.recruiter, self.outsider, self.admin])
            db.flush()
            self.org = Employer(
                owner_user_id=self.recruiter.user_id, org_name="Launch Audit Health",
                org_type="agency", city="Dallas", state_code="TX",
            )
            self.provider = Profile(
                first_name="Avery", last_name="Nurse", profession_type="RN",
                specialty="ICU", provider_category="Nursing", job_category="Registered Nurse",
                email="avery@example.test", phone="+15555550123", city="Dallas",
                state_code="TX", zip_code="75201", years_experience=7,
                npi_number="1234567890", source=ProfileSource.json_import,
                is_listable=True,
            )
            self.provider.rebuild_search_text()
            db.add_all([self.org, self.provider])
            db.commit()
            self.recruiter_id = self.recruiter.user_id
            self.outsider_id = self.outsider.user_id
            self.admin_id = self.admin.user_id
            self.org_id = self.org.employer_id
            self.provider_id = self.provider.profile_id

        self.recruiter_headers = self._headers(self.recruiter_id, "recruiter")
        self.outsider_headers = self._headers(self.outsider_id, "recruiter")
        self.admin_headers = self._headers(self.admin_id, "admin")

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.engine.dispose()

    @staticmethod
    def _headers(user_id: str, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user_id, role)}"}

    def test_recruiter_directory_saved_search_pool_outreach_and_credits(self):
        directory = self.client.get(
            "/api/profiles", params={"providers_only": "true", "q": "registered nurse", "zip": "75201"},
            headers=self.recruiter_headers,
        )
        self.assertEqual(directory.status_code, 200, directory.text)
        self.assertEqual(directory.json()["total"], 1)
        self.assertEqual(directory.json()["items"][0]["npi_number"], "1234567890")

        saved = self.client.post("/api/saved-searches", headers=self.recruiter_headers, json={
            "name": "Dallas RNs", "params": {"q": "registered nurse", "zip": "75201", "junk": "ignored"},
        })
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertNotIn("junk", saved.json()["params"])
        saved_id = saved.json()["search_id"]
        self.assertEqual(self.client.get(
            f"/api/saved-searches/{saved_id}/matches", headers=self.recruiter_headers,
        ).json()["matches"], 1)

        pool = self.client.post("/api/pools", headers=self.recruiter_headers, json={
            "name": "Launch ICU", "visibility": "private",
        })
        self.assertEqual(pool.status_code, 201, pool.text)
        pool_id = pool.json()["pool_id"]
        added = self.client.post(f"/api/pools/{pool_id}/members", headers=self.recruiter_headers,
                                 json={"profile_ids": [self.provider_id], "stage": "sourced"})
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(added.json()["added"], 1)
        moved = self.client.patch(
            f"/api/pools/{pool_id}/members/{self.provider_id}",
            headers=self.recruiter_headers, json={"stage": "contacted", "note": "Audit"},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        export = self.client.get(f"/api/pools/{pool_id}/export.csv", headers=self.recruiter_headers)
        self.assertEqual(export.status_code, 200, export.text)
        self.assertIn("text/csv", export.headers["content-type"])

        template = self.client.post("/api/outreach/templates", headers=self.recruiter_headers, json={
            "name": "Launch template", "subject": "ICU role", "body": "Hello {{first_name}}",
        })
        self.assertEqual(template.status_code, 201, template.text)
        campaign = self.client.post("/api/outreach/campaigns", headers=self.recruiter_headers, json={
            "name": "Launch preview", "pool_id": pool_id,
            "template_id": template.json()["template_id"],
        })
        self.assertEqual(campaign.status_code, 201, campaign.text)
        self.assertEqual(campaign.json()["ready_to_send"], 0)
        detail = self.client.get(
            f"/api/outreach/campaigns/{campaign.json()['campaign_id']}",
            headers=self.recruiter_headers,
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["skip_reasons"], {"contact not released": 1})

        credits = self.client.get("/api/credits", headers=self.recruiter_headers)
        self.assertEqual(credits.status_code, 200, credits.text)
        self.assertIn("balance", credits.json())
        self.assertEqual(self.client.get(
            "/api/credits/usage", headers=self.recruiter_headers).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/analytics/funnel", headers=self.recruiter_headers).status_code, 200)

    def test_job_order_and_organization_permissions(self):
        dashboard = self.client.get("/api/employers/me/dashboard", headers=self.recruiter_headers)
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        job = self.client.post(
            "/api/jobs", params={"employer_id": self.org_id}, headers=self.recruiter_headers,
            json={"title": "Launch ICU RN", "profession_type": "RN", "specialty": "ICU",
                  "job_type": "travel", "city": "Dallas", "state_code": "TX",
                  "pay_rate_min": 2200, "pay_rate_max": 2600, "pay_unit": "weekly"},
        )
        self.assertEqual(job.status_code, 201, job.text)
        job_id = job.json()["job_id"]
        updated = self.client.patch(
            f"/api/jobs/{job_id}", headers=self.recruiter_headers,
            json={"is_urgent": True, "status": "paused"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertTrue(updated.json()["is_urgent"])
        self.assertEqual(self.client.get(
            f"/api/employers/{self.org_id}/members", headers=self.recruiter_headers,
        ).status_code, 200)
        self.assertEqual(self.client.get(
            f"/api/employers/{self.org_id}/usage", headers=self.recruiter_headers,
        ).status_code, 200)

        self.assertEqual(self.client.post(
            "/api/jobs", params={"employer_id": self.org_id}, headers=self.outsider_headers,
            json={"title": "Forbidden", "job_type": "travel"},
        ).status_code, 403)
        self.assertEqual(self.client.get(
            f"/api/employers/{self.org_id}/members", headers=self.outsider_headers,
        ).status_code, 403)
        self.assertEqual(self.client.delete(
            f"/api/jobs/{job_id}", headers=self.recruiter_headers).status_code, 204)

    def test_admin_console_and_role_boundaries(self):
        for path in ("/api/admin/overview", "/api/admin/users", "/api/admin/organizations",
                     "/api/admin/jobs", "/api/admin/logins", "/api/admin/audit"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.admin_headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.client.get(path, headers=self.recruiter_headers).status_code, 403)
                self.assertEqual(self.client.get(path).status_code, 401)

        detail = self.client.get(
            f"/api/admin/users/{self.recruiter_id}", headers=self.admin_headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        adjusted = self.client.post(
            f"/api/admin/users/{self.recruiter_id}/credits", headers=self.admin_headers,
            json={"amount": 5, "note": "Launch audit"},
        )
        self.assertEqual(adjusted.status_code, 200, adjusted.text)
        self.assertEqual(adjusted.json()["balance"], 5)
        integration = self.client.patch(
            f"/api/admin/users/{self.recruiter_id}", headers=self.admin_headers,
            json={"medhunt_nexus_enabled": True},
        )
        self.assertEqual(integration.status_code, 200, integration.text)
        self.assertEqual(self.client.patch(
            f"/api/admin/users/{self.admin_id}", headers=self.admin_headers,
            json={"status": "suspended"},
        ).status_code, 400)


if __name__ == "__main__":
    unittest.main()
