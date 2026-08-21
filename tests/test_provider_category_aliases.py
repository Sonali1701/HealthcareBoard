import unittest

from app.routers.profiles import (
    _canonical_provider_category,
    _fold_provider_category_counts,
)


class ProviderCategoryAliasTests(unittest.TestCase):
    def test_legacy_other_aliases_are_canonicalized(self):
        for value in ("Other", "Others", "other", "others", "OTHER"):
            self.assertEqual(_canonical_provider_category(value), "Others")

    def test_legacy_and_canonical_counts_share_the_others_tab(self):
        counts = _fold_provider_category_counts([
            ("Physicians", 3),
            ("Nursing", 5),
            ("Other", 7),
            ("Others", 11),
            (None, 13),
        ])

        self.assertEqual(counts["Physicians"], 3)
        self.assertEqual(counts["Nursing"], 5)
        self.assertEqual(counts["Others"], 18)
        self.assertNotIn("Other", counts)


if __name__ == "__main__":
    unittest.main()
