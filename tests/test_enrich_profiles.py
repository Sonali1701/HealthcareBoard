import unittest

from app.enrich_profiles import _normalise


class EnrichmentValidationTests(unittest.TestCase):
    def test_npi_is_never_saved_as_work_authorization(self):
        self.assertIsNone(_normalise({"work_authorization": "NPI: 1043204035"}))

    def test_real_work_authorization_is_preserved(self):
        result = _normalise({"work_authorization": "US Citizen"})
        self.assertEqual(result["work_authorization"], "US Citizen")
