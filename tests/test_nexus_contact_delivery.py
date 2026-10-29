from types import SimpleNamespace
import unittest
from unittest import mock

from app.services import nexus_contact_delivery as delivery
from app.models import Profile


IDENTITY = {
    "first_name": "Jane", "last_name": "Doe",
    "email": "jane@example.com", "phone": "5551234567",
    "npi": "1234567890", "city": "Austin", "state": "TX", "zip": "78701",
    "profession": "RN", "specialty": "ICU",
}


class FakeClient:
    instances = []

    def __init__(self, values):
        self.searches = []
        self.creates = []
        self.matches = values.get("matches", {})
        self.instances.append(self)

    def close(self):
        pass

    def search(self, field, value):
        self.searches.append((field, value))
        return self.matches.get(field, [])

    def master(self, name):
        values = {
            "states": [{"id": 1, "code": "TX"}],
            "countries": [{"id": 2, "code": "US"}],
            "candidatestatuses": [{"id": 3, "name": "PROSPECT"}],
            "referralsources": [{"id": 4, "name": "MedHunt"}],
            "professions": [{"id": 5, "name": "RN"}],
            "specialties": [{"id": 6, "name": "ICU"}],
        }
        return values[name]

    def request(self, method, path, **kwargs):
        self.creates.append((method, path, kwargs))
        return SimpleNamespace(json=lambda: {"candidateId": 99})


def _row():
    return SimpleNamespace(payload=dict(IDENTITY))


class NexusContactDeliveryTests(unittest.TestCase):
    def test_queue_snapshot_keeps_original_resume_reference(self):
        profile = Profile(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            resume_url="/files/private/resume.pdf",
        )
        self.assertEqual(
            delivery._snapshot(profile)["resume_url"],
            "/files/private/resume.pdf",
        )

    def test_existing_email_or_phone_stops_every_nexus_write(self):
        FakeClient.instances.clear()
        values = {"matches": {"email": [{"id": 7}], "phone": []}}
        with mock.patch.object(delivery.ats_connections, "settings", return_value=values), \
             mock.patch.object(delivery, "NexusClient", FakeClient), \
             mock.patch.object(delivery, "_resume_attachment") as attachment:
            row = _row()
            row.payload["resume_url"] = "/files/private/resume.pdf"
            status, candidate_id = delivery._deliver(row, SimpleNamespace())

        client = FakeClient.instances[0]
        self.assertEqual(status, "already_present")
        self.assertIsNone(candidate_id)
        self.assertEqual(client.searches, [
            ("email", IDENTITY["email"]), ("phone", IDENTITY["phone"]),
        ])
        self.assertEqual(client.creates, [])
        attachment.assert_not_called()

    def test_new_contact_is_created_once_after_both_duplicate_checks(self):
        FakeClient.instances.clear()
        values = {
            "matches": {},
            "default_profile": {"jobTypeIds": ["PERM"], "referralSourceName": "MedHunt"},
        }
        with mock.patch.object(delivery.ats_connections, "settings", return_value=values), \
             mock.patch.object(delivery, "NexusClient", FakeClient):
            status, candidate_id = delivery._deliver(_row(), SimpleNamespace())

        client = FakeClient.instances[0]
        self.assertEqual(status, "delivered")
        self.assertEqual(candidate_id, "99")
        self.assertEqual(client.searches, [
            ("email", IDENTITY["email"]), ("phone", IDENTITY["phone"]),
        ])
        self.assertEqual(len(client.creates), 1)

    def test_original_pdf_is_used_for_a_new_nexus_candidate(self):
        identity = dict(IDENTITY, resume_url="/files/private/resume.pdf")
        with mock.patch.object(delivery.storage, "key_from_url",
                               return_value=("private/resume.pdf", False)), \
             mock.patch.object(delivery.storage, "download_bytes",
                               return_value=b"%PDF-1.4 original"):
            filename, content = delivery._resume_attachment(identity)
        self.assertEqual(filename, "MedHunt_Provider_Resume.pdf")
        self.assertEqual(content, b"%PDF-1.4 original")
