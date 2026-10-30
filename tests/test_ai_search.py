"""Adversarial provider AI-search parsing and result tests."""
from __future__ import annotations

import random
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Profile, User
from app.models.enums import UserRole, UserStatus
from app.routers.profiles import (
    CopilotQuery,
    _copilot_filters,
    _copilot_rule_filters,
    _merge_copilot_filters,
    copilot_search,
)


class ProviderAiParsingTests(unittest.TestCase):
    def test_common_phrases_are_structured_without_an_llm(self):
        cases = (
            ("RN in CA", {"state_code": "CA", "license_title": "RN"}),
            ("RN with 10 years experience", {"min_experience": 10}),
            ("RN with more than 5 years", {"min_experience": 6}),
            ("RN under 2 years", {"max_experience": 1}),
            ("new grad RN", {"max_experience": 1}),
            ("RN between 5 and 10 years", {"min_experience": 5, "max_experience": 10}),
            ("not RN, I meant LPN", {"license_title": "LPN"}),
            ("nurse practitioners in Austin Texas", {"city": "austin", "state_code": "TX"}),
            ("providers with resumes", {"resume_status": "with_resume"}),
            ("providers without resumes", {"resume_status": "without_resume"}),
            ("NPI 1234567890", {"npi_number": "1234567890"}),
            ("RN with both email and phone", {"contact_available": "both"}),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                parsed = _copilot_rule_filters(query)
                for key, value in expected.items():
                    self.assertEqual(parsed.get(key), value)

    def test_incomplete_directory_features_are_not_applied_as_filters(self):
        self.assertNotIn("compact", _copilot_rule_filters("compact ICU nurses"))
        self.assertNotIn("travel_experience", _copilot_rule_filters("travel nurses"))

    def test_model_output_is_constrained(self):
        for field in ("q", "city", "worked_at"):
            self.assertNotIn(field, _copilot_filters({field: "%_"}))
        self.assertNotIn("zip", _copilot_filters({"zip": "abc12345"}))
        parsed = _copilot_filters({"license_title": "RN", "category": "Physician"})
        self.assertEqual(parsed["category"], "Nursing")
        self.assertNotIn("compact", _copilot_filters({"compact": True}))

    def test_conflicting_refinement_replaces_old_bound(self):
        merged = _merge_copilot_filters({"min_experience": 10}, {"max_experience": 2})
        self.assertNotIn("min_experience", merged)
        self.assertEqual(merged["max_experience"], 2)

    def test_malformed_queries_never_crash_parser(self):
        rng = random.Random(42)
        parts = ["RN", "LPN", "New York", "TX", "under", "not", "ICU",
                 "5+ years", "!!!", "%_", "95630", "😊", "with resume"]
        for _ in range(250):
            query = " ".join(rng.choice(parts) for _ in range(rng.randrange(24)))
            _copilot_filters(_copilot_rule_filters(query))


class ProviderAiResultTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.recruiter = User(email="ai-recruiter@example.com", role=UserRole.recruiter,
                              status=UserStatus.active)
        self.db.add(self.recruiter)
        self.db.add_all([
            Profile(first_name="Ava", last_name="Lee", profession_type="RN",
                    specialty="ICU", job_category="Nursing", state_code="TX",
                    years_experience=10, resume_url="https://example.test/ava.pdf",
                    npi_number="1234567890", is_listable=True),
            Profile(first_name="Ben", last_name="Ray", profession_type="RN",
                    specialty="ICU", job_category="Nursing", state_code="CA",
                    years_experience=2, resume_url=None, npi_number="0987654321",
                    is_listable=True),
        ])
        self.db.commit()
        self.llm_off = patch("app.config.settings.llm_enabled", False)
        self.llm_off.start()

    def tearDown(self):
        self.llm_off.stop()
        self.db.close()
        self.engine.dispose()

    def search(self, message: str, context: dict | None = None):
        return copilot_search(CopilotQuery(message=message, context=context),
                              self.recruiter, self.db)

    def test_results_honor_role_location_experience_resume_and_npi(self):
        result = self.search("ICU RN in Texas with more than 5 years")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["profile_id"], self.db.query(Profile).filter_by(
            npi_number="1234567890").one().profile_id)
        self.assertEqual(self.search("providers with resumes")["total"], 1)
        self.assertEqual(self.search("providers without resumes")["total"], 1)
        self.assertEqual(self.search("NPI 0987654321")["total"], 1)

    def test_nonsense_and_conflicts_do_not_return_arbitrary_profiles(self):
        nonsense = self.search("!!!")
        self.assertEqual(nonsense["total"], 0)
        self.assertEqual(nonsense["items"], [])
        conflict = self.search("RN with at least 10 years and under 5 years")
        self.assertEqual(conflict["total"], 0)
        self.assertIn("conflicts", conflict["answer"])

    def test_removed_filters_are_disclosed_and_not_silently_applied(self):
        compact = self.search("compact ICU nurses")
        self.assertEqual(compact["total"], 2)
        self.assertIn("compact-license status", compact["answer"])

    def test_explicit_query_facts_override_model_guesses(self):
        with (patch("app.config.settings.llm_enabled", True),
              patch("app.config.settings.llm_api_key", "test-key"),
              patch("app.config.settings.llm_model", "test-model"),
              patch("app.clean_names_llm._llm", return_value={
                  "license_title": "MD", "category": "Physician", "state_code": "CA",
              })):
            result = self.search("RN in Texas")
        self.assertEqual(result["filters"]["license_title"], "RN")
        self.assertEqual(result["filters"]["category"], "Nursing")
        self.assertEqual(result["filters"]["state_code"], "TX")
        self.assertEqual(result["total"], 1)


if __name__ == "__main__":
    unittest.main()
