from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from app.services import medhunt_ceipal


class FakeCeipalClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, dict(params or {})))
        return self.responder(url, headers or {}, params or {})


class FakePostgresSession:
    def __init__(self):
        self.bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()
        self.lock_ids = []

    def get_bind(self):
        return self.bind

    def execute(self, _statement, params):
        self.lock_ids.append(params["lock_id"])


class MedhuntCeipalDuplicateTests(unittest.TestCase):
    def test_auth_accepts_ceipal_xml_token(self):
        class FakeAuthClient:
            def post(self, *_args, **_kwargs):
                return httpx.Response(200, content=b"<root><access_token>test-token</access_token></root>")

        config = {
            "base_url": "https://api.ceipal.com", "email": "ats@example.com",
            "password": "secret", "api_key": "key", "enabled": True,
        }
        self.assertEqual(medhunt_ceipal._token(FakeAuthClient(), config), "test-token")

    def test_same_contact_uses_shared_cross_worker_lock(self):
        configuration = {"base_url": "https://api.ceipal.com", "email": "ats@example.com", "api_key": "secret"}
        first = FakePostgresSession()
        second = FakePostgresSession()
        medhunt_ceipal.acquire_duplicate_locks(first, {
            "emails": ["same@example.com"], "wireless_phones": ["4155550100"],
        }, configuration)
        medhunt_ceipal.acquire_duplicate_locks(second, {
            "emails": [" SAME@example.com "], "phones": ["415-555-0100"],
        }, configuration)

        self.assertTrue(set(first.lock_ids) & set(second.lock_ids))

    def test_exact_email_filter_blocks_existing_record(self):
        candidate = {
            "name": "Jamie Candidate",
            "emails": ["jamie@example.com"],
            "phones": [],
            "wireless_phones": ["(415) 555-0199"],
        }
        existing = {
            "id": "ceipal-123",
            "applicant_id": "123",
            "email": "jamie@example.com",
            "mobile_number": "4155550199",
        }

        def responder(_url, _headers, params):
            return httpx.Response(200, json={"results": [existing], "count": 1})

        client = FakeCeipalClient(responder)
        match = medhunt_ceipal._find_existing_applicant(client, "token", candidate)

        self.assertEqual(match["state"], "already_in_ceipal")
        self.assertEqual(match["applicant_id"], "123")
        self.assertIn("phone", match["matched_by"])
        self.assertEqual([params for _, params in client.calls], [{"email": "jamie@example.com"}])

    def test_ignored_email_filter_fails_closed_before_create(self):
        candidate = {"name": "Jamie Candidate", "emails": ["jamie@example.com"]}
        client = FakeCeipalClient(lambda *_: httpx.Response(
            200, json={"results": [{"id": "same", "email": "other@example.com"}]},
        ))
        with self.assertRaisesRegex(medhunt_ceipal.MedhuntCeipalError, "did not apply the email filter"):
            medhunt_ceipal._find_existing_applicant(client, "token", candidate)

    def test_complete_empty_listing_allows_create_check(self):
        client = FakeCeipalClient(lambda *_: httpx.Response(200, json={"results": [], "count": 0}))
        self.assertIsNone(medhunt_ceipal._find_existing_applicant(
            client, "token", {"emails": ["new@example.com"]},
        ))

    def test_incomplete_email_results_fail_closed(self):
        candidate = {"emails": ["on-second-page@example.com"]}
        client = FakeCeipalClient(lambda *_: httpx.Response(200, json={
            "count": 2, "results": [{"id": "1", "email": "on-second-page@example.com"}],
        }))
        with self.assertRaisesRegex(medhunt_ceipal.MedhuntCeipalError, "incomplete result"):
            medhunt_ceipal._find_existing_applicant(client, "token", candidate)

    def test_success_http_with_api_error_fails_closed(self):
        client = FakeCeipalClient(lambda *_: httpx.Response(
            200, json={"success": False, "message": "rate limited"},
        ))
        with self.assertRaisesRegex(medhunt_ceipal.MedhuntCeipalError, "rejected"):
            medhunt_ceipal._find_existing_applicant(
                client, "token", {"emails": ["new@example.com"]},
            )

    def test_create_requires_confirmed_applicant_id(self):
        class FakeCreateClient:
            def post(self, *_args, **_kwargs):
                return httpx.Response(200, json={"status": "success"})
        with self.assertRaisesRegex(medhunt_ceipal.MedhuntCeipalError, "unconfirmed"):
            medhunt_ceipal._create_applicant(
                FakeCreateClient(), "token",
                {"name": "Jamie Candidate", "emails": ["jamie@example.com"]},
            )

    def test_create_uses_plain_form_and_confirms_201_by_email(self):
        class FakeCreateClient:
            def __init__(self):
                self.form = None

            def post(self, *_args, **kwargs):
                self.form = kwargs.get("data")
                return httpx.Response(201, json={"status": 201, "success": 1})

            def get(self, _url, **_kwargs):
                return httpx.Response(200, json={"count": 1, "results": [
                    {"applicant_id": "created-123", "email": "jamie@example.com"},
                ]})

        client = FakeCreateClient()
        applicant_id = medhunt_ceipal._create_applicant(
            client, "token", {"name": "Jamie Candidate", "emails": ["jamie@example.com"]},
        )
        self.assertEqual(applicant_id, "created-123")
        self.assertEqual(client.form["standard_fields.email"], "jamie@example.com")

    def test_phone_only_candidate_fails_closed(self):
        with self.assertRaisesRegex(medhunt_ceipal.MedhuntCeipalError, "without an email"):
            medhunt_ceipal._find_existing_applicant(
                FakeCeipalClient(lambda *_: None), "token", {"phones": ["4155550100"]},
            )

    def test_existing_contact_prevents_create(self):
        candidate = {"name": "Jamie Candidate", "emails": ["jamie@example.com"]}
        client = FakeCeipalClient(lambda *_: httpx.Response(200, json={"results": []}))
        duplicate = {
            "state": "already_in_ceipal", "blocked": True, "checked": True,
            "applicant_id": "456", "status": "Active", "matched_by": ["email"],
            "match_count": 1,
        }
        with patch.object(medhunt_ceipal, "_configured", return_value=True), \
             patch.object(medhunt_ceipal.httpx, "Client") as client_factory, \
             patch.object(medhunt_ceipal, "_token", return_value="token"), \
             patch.object(medhunt_ceipal, "_find_existing_applicant", return_value=duplicate), \
             patch.object(medhunt_ceipal, "_create_applicant") as create:
            client_factory.return_value.__enter__.return_value = client
            result = medhunt_ceipal.upload_candidate(candidate)

        self.assertEqual(result["state"], "already_in_ceipal")
        self.assertTrue(result["blocked"])
        create.assert_not_called()

    def test_search_error_fails_closed(self):
        candidate = {"name": "Jamie Candidate", "emails": ["jamie@example.com"]}
        client = FakeCeipalClient(lambda *_: httpx.Response(503, json={"error": "unavailable"}))
        with self.assertRaises(medhunt_ceipal.MedhuntCeipalError):
            medhunt_ceipal._find_existing_applicant(client, "token", candidate)


if __name__ == "__main__":
    unittest.main()
