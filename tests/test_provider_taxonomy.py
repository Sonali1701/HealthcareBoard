import unittest

from app.importers.parsing import classify_provider


class ProviderTaxonomyTests(unittest.TestCase):
    def test_approved_categories(self):
        self.assertEqual(classify_provider("MD"), "Physicians")
        self.assertEqual(classify_provider(None, "Family Medicine"), "Physicians")
        self.assertEqual(classify_provider("NP"), "APP")
        self.assertEqual(classify_provider("CRNA"), "APP")
        self.assertEqual(classify_provider("RN"), "Nursing")
        self.assertEqual(classify_provider("LPN"), "Nursing")
        self.assertEqual(classify_provider("CNA"), "Nursing")
        self.assertEqual(classify_provider(None, None, "CT Technologist"), "Allied")

    def test_out_of_scope_roles_go_to_others(self):
        self.assertEqual(classify_provider("PA"), "Others")
        self.assertEqual(classify_provider("DO"), "Others")
        self.assertEqual(classify_provider("PT"), "Others")
        self.assertEqual(classify_provider("OT"), "Others")


if __name__ == "__main__":
    unittest.main()
