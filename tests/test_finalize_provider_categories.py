import unittest

from app.finalize_provider_categories import Outcome, _new_values, _strict_name
from app.reclassify_other_profiles import Decision


def row(*, listable=True, profession=None):
    return {
        "profile_id": "profile-1",
        "provider_category": "Other",
        "profession_type": profession,
        "is_listable": listable,
    }


class ProviderFinalizationTests(unittest.TestCase):
    def test_strict_name_requires_two_real_parts(self):
        self.assertTrue(_strict_name("Jane", "Doe"))
        self.assertFalse(_strict_name("Jane", None))
        self.assertFalse(_strict_name("Registered", "Nurse"))

    def test_exact_role_uses_approved_category(self):
        outcome = Outcome(
            "profile-1", Decision("Nursing", "RN", "RN"), None,
            500, True, True,
        )
        change = _new_values(
            row(), outcome, assign_others=True, hide_invalid=True,
        )
        self.assertEqual((change["category"], change["profession"]), ("Nursing", "RN"))
        self.assertTrue(change["is_listable"])

    def test_unmatched_valid_profile_becomes_listable_others(self):
        outcome = Outcome("profile-1", None, None, 500, True, True)
        change = _new_values(
            row(profession="PT"), outcome,
            assign_others=True, hide_invalid=True,
        )
        self.assertEqual((change["category"], change["profession"]), ("Others", "PT"))
        self.assertTrue(change["is_listable"])

    def test_invalid_listable_profile_is_hidden(self):
        outcome = Outcome("profile-1", None, None, 500, False, True)
        change = _new_values(
            row(), outcome, assign_others=True, hide_invalid=True,
        )
        self.assertEqual(change["category"], "Others")
        self.assertFalse(change["is_listable"])

    def test_existing_hidden_profile_is_never_unhidden(self):
        outcome = Outcome(
            "profile-1", Decision("APP", "NP", "NP"), None,
            500, True, True,
        )
        change = _new_values(
            row(listable=False), outcome,
            assign_others=True, hide_invalid=True,
        )
        self.assertEqual(change["category"], "APP")
        self.assertFalse(change["is_listable"])


if __name__ == "__main__":
    unittest.main()
