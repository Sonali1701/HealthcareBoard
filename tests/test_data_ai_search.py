"""AI Search uses the recovered Data directory and its supported filters."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.routers.recovered_data import DataCopilotQuery, _copilot_data_filters, copilot_data


class DataAiSearchTests(unittest.TestCase):
    def test_role_specialty_state_resume_and_experience(self):
        filters, notes = _copilot_data_filters("ICU RNs in Texas with resumes and 5+ years")
        self.assertEqual(filters["q"], "Registered Nurse")
        self.assertEqual(filters["category"], "Nursing")
        self.assertEqual(filters["specialty"], "ICU")
        self.assertEqual(filters["state_code"], "TX")
        self.assertEqual(filters["resume"], "with")
        self.assertEqual(filters["min_experience"], 5)
        self.assertEqual(notes, [])

    def test_followup_keeps_data_filters(self):
        first, _ = _copilot_data_filters("registered nurses with resumes")
        second, _ = _copilot_data_filters("only in Florida", first)
        self.assertEqual(second["q"], "Registered Nurse")
        self.assertEqual(second["resume"], "with")
        self.assertEqual(second["state_code"], "FL")

    @patch("app.routers.recovered_data.search_data")
    def test_endpoint_uses_data_directory_page(self, search):
        search.return_value = {"items": [{"source_record_key": "profile:test"}], "has_next": True}
        user = SimpleNamespace(role=SimpleNamespace(value="recruiter"))
        result = copilot_data(DataCopilotQuery(message="RN in California"), user)
        self.assertEqual(result["items"][0]["source_record_key"], "profile:test")
        self.assertTrue(result["has_next"])
        self.assertEqual(search.call_args.kwargs["limit"], 12)


if __name__ == "__main__":
    unittest.main()
