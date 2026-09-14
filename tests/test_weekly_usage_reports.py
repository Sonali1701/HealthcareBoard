from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    CreditTransaction,
    Employer,
    EmployerMember,
    User,
    UserRole,
    UserStatus,
    WeeklyUsageReportDelivery,
)
from app.services import credits
from app.services.weekly_usage_reports import report_period, reports_are_due, run


class WeeklyUsageReportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        db = self.Session()
        self.users = {}
        for label in ("owner", "admin", "manager", "recruiter"):
            user = User(
                email=f"{label}@test.local",
                password_hash="x",
                role=UserRole.recruiter,
                status=UserStatus.active,
            )
            db.add(user)
            db.flush()
            self.users[label] = user
        employer = Employer(
            owner_user_id=self.users["owner"].user_id,
            org_name="Test Health",
        )
        db.add(employer)
        db.flush()
        for role in ("admin", "manager", "recruiter"):
            db.add(EmployerMember(
                employer_id=employer.employer_id,
                user_id=self.users[role].user_id,
                member_role=role,
            ))
        credits.grant(db, self.users["recruiter"].user_id, 10)
        db.commit()
        credits.charge(
            db,
            self.users["recruiter"].user_id,
            "reveal_contact",
            idempotency_key="weekly-test",
            entity_id="candidate-1",
        )
        db.flush()
        txn = db.scalar(select(CreditTransaction).where(
            CreditTransaction.idempotency_key == "weekly-test"))
        txn.created_at = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
        db.commit()
        db.close()

    def tearDown(self):
        self.engine.dispose()

    def test_period_is_previous_completed_monday_to_monday(self):
        self.assertEqual(
            report_period(datetime(2026, 9, 2, 12, tzinfo=timezone.utc)),
            (datetime(2026, 8, 24).date(), datetime(2026, 8, 31).date()),
        )

    def test_monday_report_waits_until_configured_hour(self):
        self.assertFalse(reports_are_due(
            datetime(2026, 8, 31, 12, 59, tzinfo=timezone.utc)))
        self.assertTrue(reports_are_due(
            datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)))

    @patch(
        "app.services.weekly_usage_reports.email_service.send_weekly_usage_report",
        return_value=True,
    )
    def test_only_owner_and_admin_receive_one_idempotent_report(self, send):
        now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        first = run(now=now, session_factory=self.Session)
        second = run(now=now, session_factory=self.Session)

        self.assertEqual(first["sent"], 2)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(
            {call.args[0] for call in send.call_args_list},
            {"owner@test.local", "admin@test.local"},
        )
        report = send.call_args.kwargs
        recruiter = next(
            m for m in report["members"] if m["email"] == "recruiter@test.local"
        )
        self.assertEqual(recruiter["credits_used"], 1)
        self.assertEqual(recruiter["contacts_revealed"], 1)
        db = self.Session()
        deliveries = db.query(WeeklyUsageReportDelivery).all()
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(d.status == "sent" for d in deliveries))
        db.close()


if __name__ == "__main__":
    unittest.main()
