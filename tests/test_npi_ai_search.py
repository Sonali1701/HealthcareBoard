"""Natural-language NPI search must use fields the directory actually has."""
from __future__ import annotations

import unittest

from app.routers.provider_directory import _copilot_directory_filters, _conditions


class NpiAiSearchTests(unittest.TestCase):
    def test_profession_and_state_use_taxonomy(self):
        filters, ignored = _copilot_directory_filters("registered nurses in California")
        self.assertEqual(filters["q"], "registered nurses")
        self.assertEqual(filters["state_code"], "CA")
        self.assertEqual(ignored, [])
        where, params = _conditions(q=filters["q"], specialty=None,
                                    state_code=filters["state_code"],
                                    zip_code=None, radius_mi=None, center=None)
        self.assertIn("taxonomy_code", where)
        self.assertEqual(params["role_prefix"], "163W")

    def test_icu_request_discloses_unsupported_experience(self):
        filters, ignored = _copilot_directory_filters(
            "ICU nurses in California with 10+ years")
        self.assertEqual(filters["q"], "registered nurse")
        self.assertEqual(filters["specialty_text"], "critical care")
        self.assertIn("years of experience", ignored)

    def test_resume_request_is_disclosed_as_unsupported(self):
        _filters, ignored = _copilot_directory_filters("ICU nurses with resumes")
        self.assertIn("resume availability", ignored)

    def test_state_code_does_not_pollute_specialty_search(self):
        filters, _ = _copilot_directory_filters("hematology in NM")
        self.assertEqual(filters["q"], "hematology")
        self.assertEqual(filters["state_code"], "NM")

    def test_plural_roles_and_common_specialty_nouns(self):
        therapists, _ = _copilot_directory_filters("physical therapists in Texas")
        self.assertEqual(therapists["q"], "physical therapist")
        cardiologists, _ = _copilot_directory_filters("cardiologists in Florida")
        self.assertEqual(cardiologists["q"], "cardio")

    def test_followup_preserves_role_and_specialty(self):
        prior, _ = _copilot_directory_filters("ICU nurses")
        filters, _ = _copilot_directory_filters("only in Texas", prior)
        self.assertEqual(filters["q"], "registered nurse")
        self.assertEqual(filters["specialty_text"], "critical care")
        self.assertEqual(filters["state_code"], "TX")


if __name__ == "__main__":
    unittest.main()
