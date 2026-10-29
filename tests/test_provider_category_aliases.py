import unittest
from pathlib import Path
import re
import inspect
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Profile
from app.schemas.profile import ProfileCardOut
from app.routers.profiles import (
    _directory_category,
    _directory_category_expression,
    _fold_provider_category_counts,
    _provider_category_condition,
    _provider_conditions,
    _profile_card,
    _PROVIDER_CATS,
    _FACETS_CACHE,
    category_counts,
    profile_facets,
)
from app.routers.saved_searches import _clean, _count_matches


class ProviderCategoryAliasTests(unittest.TestCase):
    def test_provider_tabs_are_built_from_facets(self):
        html = (Path(__file__).resolve().parents[1] / "templates" / "launch" /
                "board.html").read_text(encoding="utf-8")
        tabs = set(re.findall(r'data-category="([^"]*)"',
                              html.split('id="provider-tabs"', 1)[1].split('</div>', 1)[0]))
        self.assertEqual(tabs, {""})
        js = (Path(__file__).resolve().parents[1] / "static" / "js" /
              "launch-board.js").read_text(encoding="utf-8")
        self.assertIn('Object.keys(S.facetCategories || {})', js)

    def test_counts_preserve_job_categories_and_group_missing_values(self):
        counts = _fold_provider_category_counts([
            ("Physician", 3),
            ("Nursing", 5),
            ("Other / Unclassified", 7),
            ("Therapy", 11),
            (None, 13),
        ])
        self.assertEqual(counts["Physician"], 3)
        self.assertEqual(counts["Nursing"], 5)
        self.assertEqual(counts["Therapy"], 11)
        self.assertEqual(counts["Other / Unclassified"], 7)
        self.assertEqual(counts["Uncategorized"], 13)
        self.assertEqual(sum(counts.values()), 39)

    def test_job_categories_are_used_without_broad_grouping(self):
        self.assertEqual(len(_PROVIDER_CATS), 21)
        for job_category in _PROVIDER_CATS[:-1]:
            with self.subTest(job_category=job_category):
                self.assertEqual(_directory_category(None, job_category), job_category)
        self.assertEqual(_directory_category("Physicians", None), "Uncategorized")
        self.assertEqual(_directory_category(None, "  Therapy  "), "Therapy")

    def test_directory_hides_uncategorized_and_respects_opt_out(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            rows = [
                Profile(first_name="A", last_name="A", provider_category="Nursing", is_listable=True),
                Profile(first_name="B", last_name="B", provider_category=None,
                        job_category="Physician",
                        is_listable=False, screen_reason="not_healthcare"),
                Profile(first_name="C", last_name="C", provider_category="Unknown",
                        job_category="Behavioral Health", is_listable=False),
                Profile(first_name="D", last_name="D", provider_category="Other",
                        is_listable=False, screen_reason="opted_out"),
                Profile(first_name="E", last_name="E", provider_category="Allied",
                        is_listable=False, merged_into="another-profile"),
                Profile(first_name="F", last_name="F", provider_category=None,
                        job_category="Facility / Agency / Organization", is_listable=False),
                Profile(first_name="G", last_name="G", provider_category=None,
                        job_category="Other / Unclassified", specialty="Nurse's Aide"),
                Profile(first_name="H", last_name="H", provider_category=None,
                        job_category="Other / Unclassified", specialty="Specialist"),
                Profile(first_name="I", last_name="I", provider_category=None,
                        job_category="Student / Trainee", specialty="Resident"),
                Profile(first_name="J", last_name="J", provider_category=None,
                        job_category="Other / Unclassified", specialty="Homemaker"),
                Profile(first_name="K", last_name="K", provider_category=None,
                        job_category="Physician", specialty="Internal Medicine",
                        is_listable=True),
            ]
            db.add_all(rows)
            db.commit()
            conds = _provider_conditions(db, providers_only=True)
            all_rows = db.scalars(select(Profile).where(*conds)).all()
            self.assertEqual({p.first_name for p in all_rows},
                             {"I", "K"})
            others = db.scalars(select(Profile).where(
                *conds, _provider_category_condition("Other / Unclassified"))).all()
            self.assertEqual({p.first_name for p in others}, set())
            uncategorized = db.scalars(select(Profile).where(
                *conds, _provider_category_condition("Uncategorized"))).all()
            self.assertEqual({p.first_name for p in uncategorized}, set())
            facilities = db.scalars(select(Profile).where(
                *conds, _provider_category_condition("Facility / Agency / Organization"))).all()
            self.assertEqual({p.first_name for p in facilities}, set())
            physicians = db.scalars(select(Profile).where(
                *conds, _provider_category_condition("Physician"))).all()
            self.assertEqual({p.first_name for p in physicians}, {"K"})
            allied = db.scalars(select(Profile).where(
                *conds, _provider_category_condition("Behavioral Health"))).all()
            self.assertEqual({p.first_name for p in allied}, set())
            self.assertEqual(_profile_card(physicians[0], released=False)["provider_category"],
                             "Physician")
            self.assertEqual(_profile_card(physicians[0], released=False)["job_category"],
                             "Physician")
            self.assertEqual(ProfileCardOut.model_validate(
                _profile_card(physicians[0], released=False)).job_category, "Physician")
            self.assertIsNone(_profile_card(rows[0], released=False)["job_category"])
            self.assertEqual(_profile_card(rows[0], released=False)["provider_category"],
                             "Uncategorized")
            counts = _fold_provider_category_counts(db.execute(
                select(_directory_category_expression(), func.count())
                .where(*conds).group_by(_directory_category_expression())).all())
            self.assertEqual(sum(counts.values()), len(all_rows))
            self.assertEqual(counts["Physician"], 1)
            self.assertEqual(counts.get("Behavioral Health", 0), 0)
            params = {name: None for name in inspect.signature(category_counts).parameters
                      if name not in {"db", "user"}}
            user = SimpleNamespace(role=SimpleNamespace(value="recruiter"))
            self.assertEqual(category_counts(db, user, **params), counts)
            _FACETS_CACHE["data"] = None
            self.assertEqual(profile_facets(user, db)["categories"], counts)
            _FACETS_CACHE["data"] = None
        finally:
            db.close()
            engine.dispose()

    def test_registered_nurse_search_excludes_physician_mentions_and_needs_no_resume(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            profiles = [
                Profile(first_name="Registered", last_name="Nurse",
                        profession_type="RN", provider_category="Nursing",
                        job_category="Nursing",
                        specialty="ICU", resume_url=None),
                Profile(first_name="Former", last_name="Nurse",
                        profession_type="MD", provider_category="Physicians",
                        job_category="Physician",
                        bio="Previously worked as a registered nurse in ICU."),
                Profile(first_name="Nurse", last_name="Anesthetist",
                        profession_type="CRNA", provider_category="APP",
                        job_category="Nursing",
                        specialty="Certified Registered Nurse Anesthetist"),
            ]
            for profile in profiles:
                profile.rebuild_search_text()
            db.add_all(profiles)
            db.commit()
            conds = _provider_conditions(db, providers_only=True, q="registered nurse ICU")
            found = db.scalars(select(Profile).where(*conds)).all()
            self.assertEqual([p.first_name for p in found], ["Registered"])
            self.assertIsNone(found[0].resume_url)
            self.assertFalse(_profile_card(found[0], released=False)["has_resume_file"])
            self.assertEqual(db.scalar(select(func.count()).select_from(Profile).where(
                *conds, _provider_category_condition("Physicians"))), 0)
            anesthetists = db.scalars(select(Profile).where(*_provider_conditions(
                db, providers_only=True, q="registered nurse anesthetist"))).all()
            self.assertEqual([p.first_name for p in anesthetists], ["Nurse"])
        finally:
            db.close()
            engine.dispose()

    def test_provider_search_uses_name_specialty_and_job_category_not_search_text(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add_all([
                Profile(first_name="Alice", last_name="Hart", specialty="ICU",
                        job_category="Nursing", city="Chicago", search_text="outdated"),
                Profile(first_name="Ben", last_name="Vale", specialty="Cardiac",
                        job_category="Therapy", search_text="icu nursing alice"),
            ])
            db.commit()

            def names(q):
                return {p.first_name for p in db.scalars(select(Profile).where(
                    *_provider_conditions(db, providers_only=True, q=q)))}

            self.assertEqual(names("Alice Hart"), {"Alice"})
            self.assertEqual(names("icu"), {"Alice"})
            self.assertEqual(names("Nursing"), {"Alice"})
            self.assertEqual(names("Therapy"), {"Ben"})
            self.assertEqual(names("Chicago"), set())
            self.assertEqual(names("outdated"), set())
        finally:
            db.close()
            engine.dispose()

    def test_multiword_role_search_does_not_combine_unrelated_fields(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add_all([
                Profile(first_name="Nurse", last_name="Smith",
                        specialty="Family Practitioner", job_category="Physician"),
                Profile(first_name="Maya", last_name="Patel",
                        specialty="Family Nurse Practitioner", job_category="Nursing"),
                Profile(first_name="Iris", last_name="Gardner",
                        specialty="Adult Medicine Physician", job_category="Physician"),
                Profile(first_name="David", last_name="Cole",
                        specialty="Adult Medicine Physician", job_category="Physician",
                        search_text="nurse practitioner"),
                Profile(first_name="Lola Nurse Practitioner", last_name="Lola Nurse Practitioner",
                        specialty="Adult Medicine Physician", job_category="Physician"),
                Profile(first_name="Jude", last_name="Stone",
                        specialty="Cardiovascular Care, Family Nurse Practitioner",
                        job_category="Physician"),
                Profile(first_name="Penny", last_name="Ash",
                        specialty="Physician Assistant", job_category="Physician Assistant"),
                Profile(first_name="Nia", last_name="Brown",
                        specialty="Cardiology", job_category="Nursing"),
                Profile(first_name="Phil", last_name="Taylor",
                        specialty="Cardiology", job_category="Physician"),
                Profile(first_name="Ana", last_name="Bell",
                        specialty="Counselor",
                        job_category="Mental & Behavioural Health"),
            ])
            db.commit()

            def matching(q):
                return {p.first_name for p in db.scalars(select(Profile).where(
                    *_provider_conditions(db, providers_only=True, q=q)))}

            # Cross-field role word matches and stale search text do not qualify.
            self.assertEqual(matching("nurse practitioner"), {"Maya"})
            self.assertEqual(matching("family nurse practitioner"), {"Maya"})
            self.assertEqual(matching("maya patel"), {"Maya"})
            self.assertEqual(matching("mental behavioural health"), {"Ana"})
            self.assertEqual(matching("adult medicine physician"),
                             {"Iris", "David", "Lola Nurse Practitioner"})
            self.assertEqual(matching("physician"), {"Nurse", "Iris", "David", "Lola Nurse Practitioner", "Jude", "Phil"})
            self.assertEqual(matching("cardiology"), {"Nia", "Phil"})
            user = SimpleNamespace(role=SimpleNamespace(value="recruiter"))
            defaults = {name: None for name in inspect.signature(category_counts).parameters
                        if name not in {"db", "user"}}
            counts = category_counts(db, user, **{**defaults, "q": "nurse practitioner"})
            self.assertEqual(counts["Nursing"], 1)
            self.assertEqual(counts["Physician"], 0)
        finally:
            db.close()
            engine.dispose()

    def test_short_names_search_exactly_and_stale_role_text_is_ignored(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add_all([
                Profile(first_name="Jo", last_name="Smith",
                        specialty="Nurse Practitioner", job_category="Nursing"),
                Profile(first_name="Alex", last_name="Smith",
                        specialty="Internal Medicine", job_category="Physician",
                        headline="Nurse Practitioner", bio="Worked with nurse practitioners",
                        search_text="nurse practitioner jo smith"),
            ])
            db.commit()

            def names(query):
                return {p.first_name for p in db.scalars(select(Profile).where(
                    *_provider_conditions(db, providers_only=True, q=query)))}

            self.assertEqual(names("Jo"), {"Jo"})
            self.assertEqual(names("Jo Smith"), {"Jo"})
            self.assertEqual(names("nurse practitioner"), {"Jo"})
        finally:
            db.close()
            engine.dispose()

    def test_resume_filter_keeps_results_counts_and_saved_searches_aligned(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add_all([
                Profile(first_name="Alice", last_name="A", job_category="Nursing",
                        specialty="Registered Nurse",
                        resume_url="https://example.com/alice.pdf"),
                Profile(first_name="Ben", last_name="B", job_category="Nursing",
                        specialty="Registered Nurse",
                        resume_url=None),
                Profile(first_name="Cara", last_name="C", job_category="Therapy",
                        specialty="Physical Therapist",
                        resume_url="  "),
            ])
            db.commit()
            self.assertFalse(_profile_card(db.scalars(select(Profile).where(
                Profile.first_name == "Cara")).one(), released=False)["has_resume_file"])
            user = SimpleNamespace(role=SimpleNamespace(value="recruiter"))
            defaults = {name: None for name in inspect.signature(category_counts).parameters
                        if name not in {"db", "user"}}
            for status, expected_names, expected_counts in (
                ("with_resume", {"Alice"}, {"Nursing": 1}),
                ("without_resume", {"Ben", "Cara"}, {"Nursing": 1, "Therapy": 1}),
            ):
                with self.subTest(status=status):
                    conds = _provider_conditions(db, providers_only=True,
                                                 resume_status=status)
                    names = {p.first_name for p in db.scalars(select(Profile).where(*conds))}
                    self.assertEqual(names, expected_names)
                    counts = category_counts(db, user, **{**defaults, "resume_status": status})
                    self.assertEqual({k: v for k, v in counts.items() if v}, expected_counts)
                    self.assertEqual(_count_matches(db, {"resume_status": status}),
                                     len(expected_names))
            self.assertEqual(_clean({"resume_status": "with_resume"}),
                             {"resume_status": "with_resume"})
            self.assertEqual(_clean({"resume_status": "invalid"}), {})
        finally:
            db.close()
            engine.dispose()

    def test_state_facets_exclude_malformed_values(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add_all([
                Profile(first_name="A", last_name="A", state_code="TX"),
                Profile(first_name="B", last_name="B", state_code="00"),
                Profile(first_name="C", last_name="C", state_code="(M"),
            ])
            db.commit()
            user = SimpleNamespace(role=SimpleNamespace(value="recruiter"))
            _FACETS_CACHE["data"] = None
            self.assertEqual(profile_facets(user, db)["states"], ["TX"])
        finally:
            _FACETS_CACHE["data"] = None
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
