from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.database import Base, utcnow
from app.models import AuditLog, Employer, Notification, User, UserRole, UserStatus
from app.routers import analytics, extension


def request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/api/extension/auth",
        "headers": [], "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80), "scheme": "http",
    })


class MedhuntExtensionAuthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = User(
            email="recruiter@example.com", password_hash="x",
            role=UserRole.recruiter, status=UserStatus.active,
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.routers.extension.send_medhunt_login_code", return_value=True)
    def test_email_code_issues_extension_token_and_is_one_time(self, send):
        started = extension.request_medhunt_code(
            extension.MedhuntCodeRequest(email=self.user.email),
            request(), self.db, None,
        )
        code = send.call_args.args[1]
        self.assertRegex(code, r"^\d{6}$")
        verified = extension.verify_medhunt_code(
            extension.MedhuntCodeVerify(
                email=self.user.email, code=code,
                challenge=started["challenge"],
            ),
            request(), self.db, None,
        )
        self.assertEqual(verified["user"]["user_id"], self.user.user_id)
        self.assertTrue(verified["extension_token"])

        with self.assertRaises(HTTPException) as replay:
            extension.verify_medhunt_code(
                extension.MedhuntCodeVerify(
                    email=self.user.email, code=code,
                    challenge=started["challenge"],
                ),
                request(), self.db, None,
            )
        self.assertEqual(replay.exception.status_code, 401)

    @patch("app.routers.extension.send_medhunt_login_code", return_value=True)
    def test_unknown_email_has_generic_response_and_sends_nothing(self, send):
        result = extension.request_medhunt_code(
            extension.MedhuntCodeRequest(email="unknown@example.com"),
            request(), self.db, None,
        )
        self.assertIn("If this email", result["detail"])
        self.assertTrue(result["challenge"])
        send.assert_not_called()

    def test_enrichment_activity_is_idempotent_and_attributed(self):
        body = extension.MedhuntEnrichmentEvent(
            event_id="event_12345678", candidate_id="42", status="found",
            source="npiprofile", run_id="run_12345678",
        )
        first = extension.record_medhunt_enrichment(body, self.user, self.db, request())
        second = extension.record_medhunt_enrichment(body, self.user, self.db, request())
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        logs = self.db.scalars(select(AuditLog).where(
            AuditLog.action == extension.MEDHUNT_ENRICHED_ACTION,
        )).all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].actor_user_id, self.user.user_id)
        self.assertEqual(logs[0].meta["candidate_id"], "42")
        summary = analytics.sourcing_activity(self.user, self.db, days=30)
        self.assertEqual(summary["medhunt"]["enriched_total"], 1)
        self.assertEqual(summary["medhunt"]["attempts_recent"], 1)
        detail = analytics.medhunt_extension_activity(self.user, self.db, days=30)
        self.assertEqual(detail["summary"]["users"], 1)
        self.assertEqual(detail["summary"]["candidates_enriched"], 1)
        self.assertEqual(detail["sources"], [{"source": "npiprofile", "checks": 1, "enriched": 1}])
        self.assertEqual(detail["activity"][0]["candidate_id"], "42")

    @patch.object(extension.settings, "medhunt_service_token", "test-shared-token")
    def test_service_replay_records_platform_provider_and_event_time(self):
        req = Request({
            "type": "http", "method": "POST", "path": "/api/extension/activity/enrichment/service",
            "headers": [(b"x-medhunt-service-token", b"test-shared-token")],
            "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
            "scheme": "http",
        })
        occurred = utcnow() - timedelta(hours=2)
        body = extension.MedhuntServiceEnrichmentEvent(
            user_id=self.user.user_id, event_id="replay_event_12345678",
            candidate_id="42", status="found", source="indeed",
            platform="indeed", provider="quick_sourcer", occurred_at=occurred,
        )
        result = extension.record_medhunt_enrichment_service(body, self.db, req)
        self.assertTrue(result["recorded"])
        self.assertFalse(extension.record_medhunt_enrichment_service(body, self.db, req)["recorded"])
        event = self.db.scalar(select(AuditLog).where(AuditLog.entity_id == body.event_id))
        self.assertEqual(event.meta["platform"], "indeed")
        self.assertEqual(event.meta["provider"], "quick_sourcer")
        self.assertEqual(event.created_at, occurred)
        detail = analytics.medhunt_extension_activity(
            self.user, self.db, days=30, platform="indeed", provider="quick_sourcer",
        )
        self.assertEqual(detail["summary"]["checks"], 1)
        self.assertEqual(detail["provider_options"], ["quick_sourcer"])

    def test_org_device_scope_rejects_regular_recruiter(self):
        owner = User(email="owner@example.com", role=UserRole.recruiter,
                     status=UserStatus.active)
        self.db.add(owner)
        self.db.flush()
        employer = Employer(owner_user_id=owner.user_id, org_name="Example Staffing")
        self.db.add(employer)
        self.db.commit()
        scope, scoped_org = extension._medhunt_scope(
            self.db, owner, employer_id=employer.employer_id, device_admin=True,
        )
        self.assertEqual(scoped_org.employer_id, employer.employer_id)
        self.assertIn(owner.user_id, scope["user_ids"])
        with self.assertRaises(HTTPException) as denied:
            extension._medhunt_scope(
                self.db, self.user, employer_id=employer.employer_id,
                device_admin=True,
            )
        self.assertEqual(denied.exception.status_code, 403)

    @patch("app.routers.extension._medhunt_request")
    def test_halo_assignment_updates_extension_and_notifies_recruiter(self, remote):
        employer = Employer(owner_user_id=self.user.user_id, org_name="Example Staffing")
        recruiter = User(email="teammate@example.com", role=UserRole.recruiter,
                         status=UserStatus.active)
        self.db.add_all([employer, recruiter])
        self.db.flush()
        from app.models import EmployerMember
        self.db.add(EmployerMember(employer_id=employer.employer_id,
                                   user_id=recruiter.user_id))
        self.db.commit()
        remote.side_effect = [
            {"id": 12, "candidate_id": 34, "initiated_by": self.user.user_id,
             "candidate_name": "A Candidate", "messages": [
                 {"direction": "inbound", "body": "Interested"},
             ]},
            {"conversation": {"assigned_recruiter_id": recruiter.user_id}},
        ]
        result = extension.assign_medhunt_conversation(
            extension.MedhuntAssignment(
                conversation_id="12", candidate_id="34",
                recruiter_user_id=recruiter.user_id,
            ), self.user, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(result["user_id"], recruiter.user_id)
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(self.db.scalar(select(AuditLog).where(
            AuditLog.action == "medhunt_conversation_assigned",
        )).actor_user_id, self.user.user_id)
        self.assertEqual(self.db.scalar(select(Notification).where(
            Notification.user_id == recruiter.user_id,
        )).title, "Medhunt candidate reply assigned")


if __name__ == "__main__":
    unittest.main()
