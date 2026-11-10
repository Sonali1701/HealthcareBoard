"""Organization member pages stay bounded as teams grow."""
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    Application, ApplicationStatus, Employer, EmployerMember, JobPosting,
    MedhuntMessagingPermission, MedhuntSmsSender, Profile, User, UserRole,
    UserStatus,
)
from app.routers.employers import list_members, my_employer_dashboard


class OrganizationMemberLoadTests(unittest.TestCase):
    def test_summary_dashboard_keeps_kpis_without_loading_job_rows(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            owner = User(email="owner@example.com", password_hash="x",
                         role=UserRole.recruiter, status=UserStatus.active)
            db.add(owner)
            db.flush()
            org = Employer(owner_user_id=owner.user_id, org_name="Example Staffing")
            db.add(org)
            db.flush()
            jobs = [JobPosting(employer_id=org.employer_id, title=f"Role {i}")
                    for i in range(3)]
            profiles = [Profile(first_name="Candidate", last_name=str(i))
                        for i in range(3)]
            db.add_all([*jobs, *profiles])
            db.flush()
            db.add_all([
                Application(job_id=jobs[0].job_id, profile_id=profiles[0].profile_id,
                            status=ApplicationStatus.interview),
                Application(job_id=jobs[0].job_id, profile_id=profiles[1].profile_id,
                            status=ApplicationStatus.offer),
                Application(job_id=jobs[1].job_id, profile_id=profiles[2].profile_id),
            ])
            db.commit()
            full = my_employer_dashboard(owner, db)
            summary = my_employer_dashboard(owner, db, summary_only=True)
            self.assertEqual(summary["employer"], full["employer"])
            self.assertEqual(summary["kpis"], full["kpis"])
            self.assertEqual(summary["jobs"], [])
            self.assertEqual(summary["applicants"], [])
        finally:
            db.close()
            engine.dispose()

    def test_member_list_uses_bounded_queries_and_preserves_entitlements(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            owner = User(email="owner@example.com", password_hash="x",
                         role=UserRole.recruiter, status=UserStatus.active)
            users = [User(email=f"member{i}@example.com", password_hash="x",
                          role=UserRole.recruiter, status=UserStatus.active)
                     for i in range(20)]
            db.add_all([owner, *users])
            db.flush()
            org = Employer(owner_user_id=owner.user_id, org_name="Example Staffing")
            db.add(org)
            db.flush()
            db.add_all([
                EmployerMember(employer_id=org.employer_id, user_id=owner.user_id,
                               member_role="owner"),
                *(EmployerMember(employer_id=org.employer_id, user_id=u.user_id)
                  for u in users),
                MedhuntSmsSender(employer_id=org.employer_id, user_id=users[0].user_id,
                                 sender_number="+14155550101", zoom_user_id="zoom-1",
                                 updated_by_user_id=owner.user_id),
                MedhuntSmsSender(employer_id=org.employer_id, user_id=users[1].user_id,
                                 sender_number="+14155550102", zoom_user_id="zoom-2",
                                 updated_by_user_id=owner.user_id),
                MedhuntMessagingPermission(employer_id=org.employer_id,
                                           user_id=users[1].user_id, status="paused"),
            ])
            db.commit()
            queries = []
            def count_query(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    queries.append(statement)
            event.listen(engine, "before_cursor_execute", count_query)
            result = list_members(org.employer_id, owner, db)
            event.remove(engine, "before_cursor_execute", count_query)
            by_id = {row["user_id"]: row for row in result["items"]}
            self.assertEqual(len(by_id), 21)
            self.assertLessEqual(len(queries), 18)
            self.assertEqual(by_id[users[0].user_id]["messaging_status"], "enabled")
            self.assertEqual(by_id[users[1].user_id]["messaging_status"], "paused")
            self.assertEqual(by_id[users[2].user_id]["messaging_status"], "disabled")
            self.assertTrue(by_id[users[0].user_id]["can_manage_member"])
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
