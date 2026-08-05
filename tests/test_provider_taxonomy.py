import unittest

from app.importers.parsing import SPECIALTIES, _detect, classify_provider
from app.reclassify_other_profiles import classify_resume_role


class ProviderTaxonomyTests(unittest.TestCase):
    def test_field_taxonomy(self):
        self.assertEqual(classify_provider("MD"), "Physicians")
        self.assertEqual(classify_provider(None, "Family Medicine"), "Physicians")
        self.assertEqual(classify_provider("NP"), "APP")
        self.assertEqual(classify_provider("CRNA"), "APP")
        self.assertEqual(classify_provider("RN"), "Nursing")
        self.assertEqual(classify_provider("LPN"), "Nursing")
        self.assertEqual(classify_provider("CNA"), "Nursing")
        self.assertEqual(classify_provider(None, None, "CT Technologist"), "Allied")
        self.assertEqual(classify_provider(None, None, "Cardiac Cath Lab Technologist"), "Allied")
        self.assertEqual(classify_provider(None, "ICU"), "Nursing")
        self.assertEqual(classify_provider(None, "ER"), "Nursing")
        self.assertEqual(classify_provider(None, "Oncology"), "Nursing")

    def test_specialty_fallback_does_not_override_another_profession(self):
        self.assertEqual(classify_provider("MD", "ICU"), "Physicians")
        self.assertEqual(classify_provider("NP", "ER"), "APP")
        self.assertEqual(classify_provider("PharmD", "Oncology"), "Others")

    def test_out_of_scope_roles_go_to_others(self):
        self.assertEqual(classify_provider("PA"), "Others")
        self.assertEqual(classify_provider("DO"), "Others")
        self.assertEqual(classify_provider("PT"), "Others")
        self.assertEqual(classify_provider("OT"), "Others")

    def test_specialty_detection_uses_keyword_boundaries(self):
        self.assertIsNone(_detect(" curriculum vitae ", SPECIALTIES))
        self.assertEqual(_detect(" intensive care rn ", SPECIALTIES), "ICU")
        self.assertEqual(_detect(" icu/ccu registered nurse ", SPECIALTIES), "ICU")

    def test_resume_precedence(self):
        decision = classify_resume_role(
            "Jane Doe, MSN, FNP-C\nFamily Nurse Practitioner\nActive RN license"
        )
        self.assertEqual((decision.category, decision.profession), ("APP", "NP"))

        decision = classify_resume_role("Jane Doe, CRNA, RN\nNurse Anesthetist")
        self.assertEqual((decision.category, decision.profession), ("APP", "CRNA"))

        decision = classify_resume_role("John Doe, MD\nFamily Medicine\nWorked with RN staff")
        self.assertEqual((decision.category, decision.profession), ("Physicians", "MD"))

        decision = classify_resume_role("Jane Doe, RN\nCardiac Cath Lab Registered Nurse")
        self.assertEqual((decision.category, decision.profession), ("Nursing", "RN"))

        decision = classify_resume_role("Jane Doe\nCardiac Cath Lab Technologist\nBLS")
        self.assertEqual((decision.category, decision.profession),
                         ("Allied", "Cardiac Cath Lab Technologist"))

    def test_nursing_specialty_fallback(self):
        for text in (
            "ICU nurse\nFive years of intensive care experience",
            "Emergency Department\nER charge nurse",
            "Oncology nurse\nChemotherapy and oncology experience",
        ):
            decision = classify_resume_role(text)
            self.assertEqual((decision.category, decision.profession), ("Nursing", "RN"))

    def test_nursing_specialty_fallback_rejects_false_or_conflicting_roles(self):
        self.assertIsNone(classify_resume_role("Curriculum Vitae\nHealthcare professional"))
        self.assertIsNone(classify_resume_role(
            "ICU pharmacy services\nPharmD clinical pharmacist",
            profession_type="PharmD",
        ))
        decision = classify_resume_role(
            "Jane Doe, FNP-C\nICU Nurse Practitioner\nCritical care",
        )
        self.assertEqual((decision.category, decision.profession), ("APP", "NP"))


if __name__ == "__main__":
    unittest.main()
