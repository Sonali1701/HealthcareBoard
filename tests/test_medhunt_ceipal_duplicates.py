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

    def test_search_uses_wireless_phone_and_blocks_existing_record(self):
        candidate = {
            "name": "Jamie Candidate",
            "emails": ["jamie@example.com"],
            "phones": [],
            "wireless_phones": ["(415) 555-0199"],
        }
        existing = {
            "id": "ceipal-123",
            "applicant_id": "123",
            "mobile_number": "4155550199",
        }

        def responder(_url, _headers, params):
            rows = [existing] if params.get("mobile_number") == "4155550199" else []
            return httpx.Response(200, json={"results": rows})

        client = FakeCeipalClient(responder)
        match = medhunt_ceipal._find_existing_applicant(client, "token", candidate)

        self.assertEqual(match["state"], "already_in_ceipal")
        self.assertEqual(match["applicant_id"], "123")
        self.assertIn("phone", match["matched_by"])
        self.assertTrue(any(params.get("mobile_number") == "4155550199" for _, params in client.calls))

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
