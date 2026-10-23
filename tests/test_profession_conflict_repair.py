import unittest

from app.repair_profession_conflicts import decide


class ProfessionConflictRepairTests(unittest.TestCase):
    def row(self, role, category, profession):
        return {
            "resume_sections": {"raw_extracted_text":
                                role + " working in acute care with ten years of clinical experience."},
            "job_category": category,
            "profession_type": profession,
        }

    def test_source_role_corrects_wrong_profession(self):
        self.assertEqual(
            decide(self.row("Certified Registered Nurse Anesthetist", "Nursing", "MD")),
            ("CRNA", "Nursing", "source_first_crna_profession"),
        )

    def test_source_role_corrects_wrong_category(self):
        self.assertEqual(
            decide(self.row("Physician Assistant", "Physician", "PA")),
            ("PA", "Physician Assistant", "source_first_pa_category"),
        )

    def test_ambiguous_icu_physician_stays_for_review(self):
        self.assertIsNone(decide(self.row("Medical Doctor", "Nursing", "MD")))

    def test_missing_source_stays_for_review(self):
        row = self.row("Physician Assistant", "Nursing", "MD")
        row["resume_sections"] = None
        self.assertIsNone(decide(row))


if __name__ == "__main__":
    unittest.main()
