import unittest

from scripts.clean_audited_resume_names_20261120 import proposal
from scripts.audit_recovered_data_resumes import reasons


class HeaderNameTests(unittest.TestCase):
    def test_crna_surname_and_credential(self):
        self.assertEqual(proposal("Gary", "Crna", "APP", "Gary P Venet CRNA\nCertified Registered Nurse Anesthetist"),
                         ("Venet", "CRNA"))

    def test_lpn_surname(self):
        self.assertEqual(proposal("Dorothy", "Lpn", "Nursing", "Dorothy J. Dibble LPN\nOther text"),
                         ("Dibble", "LPN"))

    def test_nonclinical_and_category_conflict_stay_untouched(self):
        self.assertIsNone(proposal("Timothy", "Mcse", "APP", "Timothy Doyle, MBA, MCSE\nIT professional"))
        self.assertIsNone(proposal("Gary", "Crna", "Physician", "Gary P Venet CRNA\nOther text"))

    def test_header_must_confirm_first_name(self):
        self.assertIsNone(proposal("Gary", "Crna", "APP", "Another Person CRNA\nOther text"))

    def test_title_is_not_a_first_name(self):
        self.assertIsNone(proposal("Mrs.", "Lpn", "Nursing", "Mrs. Susan E Dries LPN"))

    def test_nurse_anesthetist_is_not_a_nursing_conflict(self):
        flags = reasons(("id", "resume", "Gary", "Venet", "CRNA",
                         "Certified Registered Nurse Anesthetist", "APP", "Los Angeles", "CA",
                         "Gary P Venet CRNA\nCertified Registered Nurse Anesthetist", "parsed", "pending"))
        self.assertNotIn("header_category_conflict", flags)


if __name__ == "__main__":
    unittest.main()
