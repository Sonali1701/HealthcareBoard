import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Profile
from app.repair_profession_conflicts import decide
from app.routers.profiles import _provider_conditions


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

    def test_unresolved_conflicts_are_hidden_from_provider_directory(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add_all([
                Profile(first_name="Wrong", last_name="One",
                        job_category="Nursing", profession_type="MD"),
                Profile(first_name="Correct", last_name="Two",
                        job_category="Nursing", profession_type="CRNA"),
                Profile(first_name="Unknown", last_name="Three",
                        job_category="Nursing", profession_type=None),
            ])
            db.commit()
            names = {p.first_name for p in db.scalars(
                select(Profile).where(*_provider_conditions(db, providers_only=True)))}
        self.assertEqual(names, {"Correct", "Unknown"})


if __name__ == "__main__":
    unittest.main()
