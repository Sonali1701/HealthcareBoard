import unittest

from app.recover_hidden_profiles import _name_key, _normalise_email, _normalise_phone


class HiddenRecoveryKeyTests(unittest.TestCase):
    def test_contact_keys_are_normalised(self):
        self.assertEqual(_normalise_email(" Jane@Example.COM "), "jane@example.com")
        self.assertEqual(_normalise_phone("+1 (212) 555-0123"), "2125550123")

    def test_name_key_ignores_case_and_punctuation(self):
        self.assertEqual(_name_key("Ta'Nyah", "O-Neal"), "tanyah oneal")


if __name__ == "__main__":
    unittest.main()
