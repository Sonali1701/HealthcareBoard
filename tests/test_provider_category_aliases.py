import unittest

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Profile
from app.routers.profiles import (
    _canonical_provider_category,
    _fold_provider_category_counts,
    _provider_category_condition,
    _provider_conditions,
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
        self.assertEqual(counts["Others"], 31)
        self.assertNotIn("Other", counts)

    def test_directory_includes_screened_and_uncategorized_but_respects_opt_out(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            rows = [
                Profile(first_name="A", last_name="A", provider_category="Nursing", is_listable=True),
                Profile(first_name="B", last_name="B", provider_category=None,
                        is_listable=False, screen_reason="not_healthcare"),
                Profile(first_name="C", last_name="C", provider_category="Unknown",
                        is_listable=False),
                Profile(first_name="D", last_name="D", provider_category="Other",
                        is_listable=False, screen_reason="opted_out"),
                Profile(first_name="E", last_name="E", provider_category="Allied",
                        is_listable=False, merged_into="another-profile"),
            ]
            db.add_all(rows)
            db.commit()
            conds = _provider_conditions(db, providers_only=True)
            all_rows = db.scalars(select(Profile).where(*conds)).all()
            self.assertEqual({p.first_name for p in all_rows}, {"A", "B", "C"})
            others = db.scalars(select(Profile).where(
                *conds, _provider_category_condition("Others"))).all()
            self.assertEqual({p.first_name for p in others}, {"B", "C"})
            counts = _fold_provider_category_counts(db.execute(
                select(Profile.provider_category, func.count())
                .where(*conds).group_by(Profile.provider_category)).all())
            self.assertEqual(sum(counts.values()), len(all_rows))
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
