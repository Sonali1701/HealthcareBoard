from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.database import Base, utcnow
from app.models import (
    AuditLog, Employer, EmployerMember, MedhuntCreditAccount,
    MedhuntCreditTransaction, MedhuntMessagingPermission, MedhuntSmsSender,
    Notification, OrganizationTeam, OrganizationTeamMember,
    User, UserRole, UserStatus,
)
from app.routers import analytics, extension


def request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/api/extension/auth",
        "headers": [], "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80), "scheme": "http",
    })


def medhunt_service_request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/api/extension/medhunt/ceipal-candidate",
        "headers": [(b"x-medhunt-service-token", b"test-service-token")],
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80), "scheme": "http",
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

    @patch("app.services.medhunt_ceipal.upload_candidate", return_value={
        "state": "already_in_ceipal", "checked": True, "applicant_id": "app-42",
        "matched_by": ["email"],
    })
    @patch("app.services.medhunt_ceipal.acquire_duplicate_locks")
    @patch("app.services.ats_connections.for_user", return_value=None)
    @patch.object(extension.settings, "medhunt_service_token", "test-service-token")
    def test_ceipal_delivery_is_visible_to_platform_admin(self, _connection, _locks, upload):
        self.user.medhunt_ceipal_enabled = True
        self.db.commit()
        result = extension.medhunt_ceipal_candidate(
            extension.MedhuntCeipalCandidate(
                user_id=self.user.user_id, candidate_id="42", name="A Candidate",
                location="Boston, MA", emails=["candidate@example.com"],
            ), medhunt_service_request(), self.db,
        )
        self.assertEqual(result["state"], "already_in_ceipal")
        upload.assert_called_once()

        admin = User(email="admin@example.com", password_hash="x",
                     role=UserRole.admin, status=UserStatus.active)
        self.db.add(admin)
        self.db.commit()
        activity = extension.list_medhunt_ceipal_deliveries(admin, self.db)
        self.assertEqual(len(activity["items"]), 1)
        self.assertEqual(activity["items"][0]["candidate_name"], "A Candidate")
        self.assertEqual(activity["items"][0]["state"], "already_in_ceipal")
        self.assertEqual(activity["items"][0]["applicant_id"], "app-42")

    def test_org_admin_manages_per_user_sms_sender_and_extension_reads_own_assignment(self):
        employer = Employer(owner_user_id=self.user.user_id, org_name="Example Staffing")
        teammate = User(email="teammate@example.com", role=UserRole.recruiter,
                        status=UserStatus.active)
        self.db.add_all([employer, teammate])
        self.db.flush()
        self.db.add(EmployerMember(employer_id=employer.employer_id,
                                   user_id=teammate.user_id))
        self.db.commit()

        saved = extension.set_medhunt_sms_sender(
            teammate.user_id,
            extension.MedhuntSmsSenderPatch(
                sender_number="(415) 555-0123", zoom_user_id="zoom-user-415",
            ),
            self.user, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(saved["sender_number"], "+14155550123")
        self.assertEqual(saved["zoom_user_id"], "zoom-user-415")
        listed = extension.list_medhunt_sms_senders(
            self.user, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(listed["items"], [{
            "user_id": teammate.user_id, "sender_number": "+14155550123",
            "zoom_user_id": "zoom-user-415",
            "messaging_status": "disabled",
        }])

        own = extension.set_medhunt_sms_sender(
            self.user.user_id,
            extension.MedhuntSmsSenderPatch(
                sender_number="+14155550124", zoom_user_id="zoom-user-own",
            ),
            self.user, self.db, employer_id=employer.employer_id,
        )
        extension.set_medhunt_messaging_permission(
            self.user.user_id,
            extension.MedhuntMessagingPermissionPatch(status="enabled"),
            self.user, self.db, employer_id=employer.employer_id,
        )
        mine = extension.my_medhunt_sms_sender(self.user, self.db)
        self.assertEqual(mine["sender_number"], own["sender_number"])
        self.assertEqual(mine["zoom_user_id"], own["zoom_user_id"])
        self.assertEqual(mine["messaging_status"], "enabled")

    @patch("app.routers.extension._medhunt_request")
    def test_assigned_user_reply_uses_their_sender_and_is_attributed(self, remote):
        employer = Employer(owner_user_id=self.user.user_id, org_name="Example Staffing")
        self.db.add(employer)
        self.db.flush()
        sender = MedhuntSmsSender(
            employer_id=employer.employer_id, user_id=self.user.user_id,
            sender_number="+14155550124", zoom_user_id="zoom-user-own",
            updated_by_user_id=self.user.user_id,
        )
        self.db.add(sender)
        self.db.commit()
        thread = {
            "id": 12, "candidate_id": "34", "initiated_by": self.user.user_id,
            "candidate_name": "A Candidate", "status": "replied",
            "messages": [{"direction": "inbound", "body": "Interested", "created": 2}],
        }
        remote.side_effect = [thread, thread]
        result = extension.reply_to_medhunt_conversation(
            12, extension.MedhuntConversationReply(
                message="Can you talk tomorrow?", request_id="reply-request-123456",
            ),
            self.user, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(result["candidate_name"], "A Candidate")
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(remote.call_args.args[0], "/internal/halo/conversations/12/reply")
        payload = remote.call_args.args[1]
        self.assertEqual(payload["actor_user_id"], self.user.user_id)
        self.assertEqual(payload["sender_number"], "+14155550124")
        self.assertEqual(payload["zoom_user_id"], "zoom-user-own")
        self.assertEqual(payload["request_id"], "reply-request-123456")

    def test_manager_controls_messaging_only_for_members_of_managed_team(self):
        manager = User(email="manager@example.com", role=UserRole.recruiter,
                       status=UserStatus.active)
        own_member = User(email="own@example.com", role=UserRole.recruiter,
                          status=UserStatus.active)
        other_member = User(email="other@example.com", role=UserRole.recruiter,
                            status=UserStatus.active)
        employer = Employer(owner_user_id=self.user.user_id, org_name="Example Staffing")
        self.db.add_all([manager, own_member, other_member, employer])
        self.db.flush()
        own_team = OrganizationTeam(employer_id=employer.employer_id, name="North",
                                    created_by_user_id=self.user.user_id)
        other_team = OrganizationTeam(employer_id=employer.employer_id, name="South",
                                      created_by_user_id=self.user.user_id)
        self.db.add_all([own_team, other_team])
        self.db.flush()
        self.db.add_all([
            EmployerMember(employer_id=employer.employer_id, user_id=manager.user_id,
                           member_role="manager"),
            EmployerMember(employer_id=employer.employer_id, user_id=own_member.user_id),
            EmployerMember(employer_id=employer.employer_id, user_id=other_member.user_id),
            OrganizationTeamMember(team_id=own_team.team_id, user_id=manager.user_id,
                                   team_role="manager", created_by_user_id=self.user.user_id),
            OrganizationTeamMember(team_id=own_team.team_id, user_id=own_member.user_id,
                                   team_role="member", created_by_user_id=self.user.user_id),
            OrganizationTeamMember(team_id=other_team.team_id, user_id=other_member.user_id,
                                   team_role="member", created_by_user_id=self.user.user_id),
            MedhuntSmsSender(employer_id=employer.employer_id, user_id=own_member.user_id,
                             sender_number="+14155550125", zoom_user_id="zoom-own",
                             updated_by_user_id=self.user.user_id),
            MedhuntSmsSender(employer_id=employer.employer_id, user_id=other_member.user_id,
                             sender_number="+14155550126", zoom_user_id="zoom-other",
                             updated_by_user_id=self.user.user_id),
        ])
        self.db.commit()

        scope, _ = extension._medhunt_scope(
            self.db, manager, employer_id=employer.employer_id,
        )
        self.assertEqual(set(scope["user_ids"]), {manager.user_id, own_member.user_id})
        enabled = extension.set_medhunt_messaging_permission(
            own_member.user_id,
            extension.MedhuntMessagingPermissionPatch(status="enabled"),
            manager, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(enabled["status"], "enabled")
        self.assertEqual(
            extension.my_medhunt_sms_sender(own_member, self.db)["sender_number"],
            "+14155550125",
        )
        paused = extension.set_medhunt_messaging_permission(
            own_member.user_id,
            extension.MedhuntMessagingPermissionPatch(
                status="paused", reason="Coaching review",
            ),
            manager, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(paused["status"], "paused")
        with self.assertRaises(HTTPException) as blocked:
            extension.my_medhunt_sms_sender(own_member, self.db)
        self.assertEqual(blocked.exception.status_code, 403)
        with self.assertRaises(HTTPException) as cross_team:
            extension.set_medhunt_messaging_permission(
                other_member.user_id,
                extension.MedhuntMessagingPermissionPatch(status="paused"),
                manager, self.db, employer_id=employer.employer_id,
            )
        self.assertEqual(cross_team.exception.status_code, 403)
        with self.assertRaises(HTTPException) as self_grant:
            extension.set_medhunt_messaging_permission(
                manager.user_id,
                extension.MedhuntMessagingPermissionPatch(status="enabled"),
                manager, self.db, employer_id=employer.employer_id,
            )
        self.assertEqual(self_grant.exception.status_code, 403)

        self.db.add_all([
            AuditLog(actor_user_id=own_member.user_id,
                     action=extension.MEDHUNT_ENRICHED_ACTION,
                     entity_type="medhunt_event", entity_id="north-event",
                     meta={"candidate_id": "north-candidate", "source": "indeed"}),
            AuditLog(actor_user_id=other_member.user_id,
                     action=extension.MEDHUNT_ENRICHED_ACTION,
                     entity_type="medhunt_event", entity_id="south-event",
                     meta={"candidate_id": "south-candidate", "source": "indeed"}),
        ])
        self.db.commit()
        report = analytics.medhunt_extension_activity(
            manager, self.db, days=30, employer_id=employer.employer_id,
        )
        self.assertEqual(report["scope"], "team")
        self.assertEqual(report["summary"]["checks"], 1)
        self.assertEqual({item["user_id"] for item in report["member_options"]},
                         {manager.user_id, own_member.user_id})

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

    def test_extension_lookup_credits_start_at_100_and_charge_once_per_candidate(self):
        self.assertEqual(extension.my_medhunt_credits(self.user, self.db), {
            "balance": 100, "lifetime_granted": 100, "lifetime_spent": 0,
        })
        body = extension.MedhuntCreditConsume(
            candidate_ids=[1, 2, 2, 3], run_id="run_12345678",
        )
        first = extension.consume_medhunt_credits(body, self.user, self.db)
        self.assertEqual(first["charged"], 3)
        self.assertEqual(first["balance"], 97)
        replay = extension.consume_medhunt_credits(body, self.user, self.db)
        self.assertEqual(replay["charged"], 0)
        self.assertEqual(replay["already_charged"], 3)
        self.assertEqual(replay["balance"], 97)
        account = self.db.scalar(select(MedhuntCreditAccount).where(
            MedhuntCreditAccount.user_id == self.user.user_id,
        ))
        self.assertEqual((account.lifetime_granted, account.lifetime_spent), (100, 3))
        ledger = self.db.scalars(select(MedhuntCreditTransaction).where(
            MedhuntCreditTransaction.user_id == self.user.user_id,
        )).all()
        self.assertEqual(len(ledger), 4)

    def test_extension_lookup_credits_fail_closed_without_negative_balance(self):
        first_hundred = extension.MedhuntCreditConsume(
            candidate_ids=list(range(1, 101)), run_id="run_12345678",
        )
        result = extension.consume_medhunt_credits(first_hundred, self.user, self.db)
        self.assertEqual((result["charged"], result["balance"]), (100, 0))
        with self.assertRaises(HTTPException) as exhausted:
            extension.consume_medhunt_credits(
                extension.MedhuntCreditConsume(
                    candidate_ids=[101], run_id="run_abcdefgh",
                ), self.user, self.db,
            )
        self.assertEqual(exhausted.exception.status_code, 402)
        self.assertEqual(extension.my_medhunt_credits(self.user, self.db)["balance"], 0)

    def test_manager_can_grant_extension_credits_only_to_organization_members(self):
        manager = User(email="manager@example.com", password_hash="x",
                       role=UserRole.recruiter, status=UserStatus.active)
        member = User(email="member@example.com", password_hash="x",
                      role=UserRole.recruiter, status=UserStatus.active)
        employer = Employer(owner_user_id=self.user.user_id, org_name="Example Staffing")
        outsider = User(email="outsider@example.com", password_hash="x",
                        role=UserRole.recruiter, status=UserStatus.active)
        self.db.add_all([manager, member, outsider, employer])
        self.db.flush()
        self.db.add_all([
            EmployerMember(employer_id=employer.employer_id, user_id=manager.user_id,
                           member_role="manager"),
            EmployerMember(employer_id=employer.employer_id, user_id=member.user_id,
                           member_role="recruiter"),
        ])
        team = OrganizationTeam(employer_id=employer.employer_id, name="General",
                                created_by_user_id=self.user.user_id)
        self.db.add(team)
        self.db.flush()
        self.db.add_all([
            OrganizationTeamMember(team_id=team.team_id, user_id=manager.user_id,
                                   team_role="manager", created_by_user_id=self.user.user_id),
            OrganizationTeamMember(team_id=team.team_id, user_id=member.user_id,
                                   team_role="member", created_by_user_id=self.user.user_id),
        ])
        self.db.commit()
        listed = extension.list_medhunt_team_credits(
            manager, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(len(listed["items"]), 2)
        result = extension.grant_medhunt_team_credits(
            member.user_id, extension.MedhuntCreditGrant(amount=25, note="Month-end top-up"),
            manager, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(result, {"balance": 125, "granted": 25})
        with self.assertRaises(HTTPException) as member_denied:
            extension.grant_medhunt_team_credits(
                manager.user_id, extension.MedhuntCreditGrant(amount=1),
                member, self.db, employer_id=employer.employer_id,
            )
        self.assertEqual(member_denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            extension.grant_medhunt_team_credits(
                outsider.user_id, extension.MedhuntCreditGrant(amount=1),
                manager, self.db, employer_id=employer.employer_id,
            )
        self.assertEqual(denied.exception.status_code, 404)

    def test_admin_can_set_selected_team_credit_balances_to_zero_then_add_in_bulk(self):
        first = User(email="first@example.com", password_hash="x",
                     role=UserRole.recruiter, status=UserStatus.active)
        second = User(email="second@example.com", password_hash="x",
                      role=UserRole.recruiter, status=UserStatus.active)
        manager = User(email="manager@example.com", password_hash="x",
                       role=UserRole.recruiter, status=UserStatus.active)
        employer = Employer(owner_user_id=self.user.user_id, org_name="Example Staffing")
        self.db.add_all([first, second, manager, employer])
        self.db.flush()
        self.db.add_all([
            EmployerMember(employer_id=employer.employer_id, user_id=first.user_id),
            EmployerMember(employer_id=employer.employer_id, user_id=second.user_id),
            EmployerMember(employer_id=employer.employer_id, user_id=manager.user_id,
                           member_role="manager"),
        ])
        self.db.commit()

        selected = [first.user_id, second.user_id]
        reset = extension.set_medhunt_team_credits_bulk(
            extension.MedhuntCreditBulkSet(user_ids=selected, balance=0, note="Quarter reset"),
            self.user, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(reset["operation"], "set")
        self.assertEqual({item["balance"] for item in reset["items"]}, {0})
        self.assertEqual({item["adjusted"] for item in reset["items"]}, {-100})

        added = extension.adjust_medhunt_team_credits_bulk(
            extension.MedhuntCreditBulkAdjust(user_ids=selected, amount=25),
            self.user, self.db, employer_id=employer.employer_id,
        )
        self.assertEqual(added["operation"], "adjust")
        self.assertEqual({item["balance"] for item in added["items"]}, {25})
        self.assertEqual({item["adjusted"] for item in added["items"]}, {25})

        with self.assertRaises(HTTPException) as denied:
            extension.set_medhunt_team_credits_bulk(
                extension.MedhuntCreditBulkSet(user_ids=selected, balance=10),
                manager, self.db, employer_id=employer.employer_id,
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
