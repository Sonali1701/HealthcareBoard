"""Provider pages expose a planner estimate while exact counts run separately."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Profile
from app.routers.profiles import search_profiles


class ProviderEstimateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = SimpleNamespace(role=SimpleNamespace(value="recruiter"),
                                    user_id="recruiter-1")
        self.db.add_all([
            Profile(first_name="Ava", last_name="Lee", specialty="ICU",
                    job_category="Nursing", is_listable=True),
            Profile(first_name="Ben", last_name="Ray", specialty="ICU",
                    job_category="Nursing", is_listable=True),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def search(self, **options):
        defaults = {name: None for name in inspect.signature(search_profiles).parameters
                    if name not in {"db", "user"}}
        defaults.update(providers_only=True, category="Nursing", limit=1, offset=0,
                        count=False, estimate=True)
        defaults.update(options)
        return search_profiles(self.db, self.user, **defaults)

    def test_estimate_is_distinct_from_exact_total(self):
        with patch("app.routers.profiles._planned_row_estimate", return_value=500):
            page = self.search()
        self.assertEqual(len(page.items), 1)
        self.assertTrue(page.has_next)
        self.assertIsNone(page.total)
        self.assertEqual(page.estimated_total, 500)

        with patch("app.routers.profiles._planned_row_estimate") as planner:
            exact = self.search(count=True)
        planner.assert_not_called()
        self.assertEqual(exact.total, 2)
        self.assertIsNone(exact.estimated_total)


if __name__ == "__main__":
    unittest.main()
