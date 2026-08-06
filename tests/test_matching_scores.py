"""Match scoring decides which candidates a recruiter sees first.

The imported requisitions carry almost no structure — no required skills, no
minimum experience — so the cases that matter most are the under-specified
ones, where a naive scorer gives everybody an identical number and the ranking
becomes meaningless.
"""
from __future__ import annotations

import unittest

from app.services.matching import (
    _Spec,
    _masked,
    _score_experience,
    _score_pay,
    _score_skills,
)


def spec(**kw):
    base = dict(specialty=None, profession_type=None, skills=set(), certs=set(),
                years_min=0, years_pref=0, state_code=None, city=None,
                pay_min=None, pay_max=None, years_declared=False)
    base.update(kw)
    return _Spec(**base)


class Profile:
    """Minimal stand-in — the scorers only read attributes."""

    def __init__(self, **kw):
        self.first_name = kw.get("first_name")
        self.last_name = kw.get("last_name")
        self.pay_min_hourly = kw.get("pay_min_hourly")


class SkillScoreTests(unittest.TestCase):
    def test_explicit_requirements_score_by_coverage(self):
        s = spec(skills={"acls", "ventilator"})
        self.assertEqual(_score_skills(s, {"acls", "ventilator"}, set()), 100.0)
        self.assertEqual(_score_skills(s, {"acls"}, set()), 50.0)
        self.assertEqual(_score_skills(s, set(), set()), 0.0)

    def test_matching_specialty_outscores_a_different_one(self):
        """With no skill list, specialty alignment has to carry the ranking."""
        s = spec(specialty="ICU")
        same = _score_skills(s, set(), set(), "ICU")
        other = _score_skills(s, set(), set(), "Home Health")
        self.assertGreater(same, other)
        self.assertGreaterEqual(same, 85.0)

    def test_documented_evidence_breaks_ties(self):
        """Two ICU nurses are not equal if one has skills on file and one does
        not — otherwise every candidate lands on the same number."""
        s = spec(specialty="ICU")
        thin = _score_skills(s, set(), set(), "ICU")
        rich = _score_skills(s, {"acls", "vent", "crrt"}, {"ccrn"}, "ICU")
        self.assertGreater(rich, thin)

    def test_scores_stay_within_bounds(self):
        s = spec(specialty="ICU")
        many = {f"skill{i}" for i in range(50)}
        self.assertLessEqual(_score_skills(s, many, many, "ICU"), 100.0)


class ExperienceScoreTests(unittest.TestCase):
    def test_undeclared_requirement_ranks_by_seniority(self):
        """Imported reqs state no minimum. A 20-year nurse must still outrank a
        1-year nurse instead of both scoring 100."""
        s = spec()
        junior = _score_experience(s, 1)
        mid = _score_experience(s, 8)
        senior = _score_experience(s, 20)
        self.assertLess(junior, mid)
        self.assertLess(mid, senior)
        self.assertLessEqual(senior, 100.0)

    def test_no_experience_is_not_zero(self):
        """Missing data is not evidence of inexperience — it must not zero out."""
        self.assertGreater(_score_experience(spec(), 0), 20.0)

    def test_declared_minimum_is_respected(self):
        s = spec(years_min=5, years_pref=8, years_declared=True)
        self.assertEqual(_score_experience(s, 10), 100.0)
        self.assertLess(_score_experience(s, 2), 70.0)

    def test_diminishing_returns_above_twenty_years(self):
        s = spec()
        self.assertEqual(_score_experience(s, 25), _score_experience(s, 20))


class PayScoreTests(unittest.TestCase):
    def test_unknown_expectation_does_not_penalise(self):
        s = spec(pay_max=70.0)
        self.assertGreaterEqual(_score_pay(s, Profile(pay_min_hourly=None)), 50.0)

    def test_asking_within_budget_scores_full(self):
        s = spec(pay_max=70.0)
        self.assertEqual(_score_pay(s, Profile(pay_min_hourly=60)), 100.0)

    def test_asking_over_budget_decays(self):
        s = spec(pay_max=70.0)
        near = _score_pay(s, Profile(pay_min_hourly=75))
        far = _score_pay(s, Profile(pay_min_hourly=120))
        self.assertGreater(near, far)


class MaskingTests(unittest.TestCase):
    """The matcher returns candidates a recruiter has not paid to see."""

    def test_name_is_reduced_to_initials(self):
        self.assertEqual(_masked(Profile(first_name="Ta'Nyah", last_name="Hoskins")), "T. H.")

    def test_partial_names_do_not_leak(self):
        self.assertEqual(_masked(Profile(first_name="Jane", last_name=None)), "J.")
        self.assertEqual(_masked(Profile(first_name=None, last_name=None)), "—")

    def test_masked_output_never_contains_the_full_name(self):
        out = _masked(Profile(first_name="Jane", last_name="Doe"))
        self.assertNotIn("Jane", out)
        self.assertNotIn("Doe", out)


if __name__ == "__main__":
    unittest.main()
