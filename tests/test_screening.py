"""The directory screen decides whether a real person stays visible.

A false hide costs a genuine candidate their visibility; a false keep puts an
IT résumé in front of a recruiter. Both matter, so the cases here are drawn
from the actual mislabelled profiles found in the imported data.
"""
from __future__ import annotations

import unittest

from app.screen_directory import (
    SCREEN_REASON_EMPTY,
    SCREEN_REASON_KEPT,
    SCREEN_REASON_NOT_HEALTHCARE,
    classify,
)

NURSE = ("Jane Doe RN. Eight years ICU experience, BLS and ACLS certified. "
         "Direct patient care in a level 1 trauma centre, med-surg float pool.")
CNA = ("Maria Lopez, Certified Nursing Assistant. Assisted patients with daily "
       "living, took vital signs and provided wound care in a skilled nursing facility.")
DEVOPS = ("Sr. AWS Cloud DevOps Engineer. Kubernetes, Docker, Terraform, CI/CD "
          "pipelines. Led a migration to microservices for a retail platform.")
QA = ("Software Quality Assurance Analyst. Selenium, JIRA, test automation "
      "frameworks, regression suites and release sign-off.")
LAWYER = ("Rutledge Law Firm PC, Attorneys at Law. Litigation support, case "
          "filings, discovery and client correspondence.")


class ScreenClassifyTests(unittest.TestCase):
    def test_clinical_resumes_are_kept(self):
        for text in (NURSE, CNA):
            keep, reason, hits = classify(text)
            self.assertTrue(keep, text[:40])
            self.assertEqual(reason, SCREEN_REASON_KEPT)
            self.assertGreaterEqual(hits, 2)

    def test_non_healthcare_resumes_are_hidden(self):
        for text in (DEVOPS, QA, LAWYER):
            keep, reason, _ = classify(text)
            self.assertFalse(keep, text[:40])
            self.assertEqual(reason, SCREEN_REASON_NOT_HEALTHCARE)

    def test_empty_and_near_empty_resumes_are_hidden(self):
        for text in ("", "   \n  ", "Resume"):
            keep, reason, hits = classify(text)
            self.assertFalse(keep)
            self.assertEqual(reason, SCREEN_REASON_EMPTY)
            self.assertEqual(hits, 0)

    def test_a_nurse_who_also_lists_software_is_kept(self):
        """Clinical staff do list Epic and SQL. Tech words must never hide them."""
        text = (NURSE + " Also proficient in Epic Systems, SQL reporting and "
                "Python for unit dashboards.")
        keep, _, _ = classify(text)
        self.assertTrue(keep)

    def test_strict_mode_needs_only_one_clinical_term(self):
        """Re-screening something the importer already called clinical: overturn
        that only when there is no clinical vocabulary at all."""
        faint = "Worked night shifts supporting patients on the unit."
        self.assertTrue(classify(faint, strict=True)[0])
        self.assertFalse(classify(DEVOPS, strict=True)[0])

    def test_strict_mode_is_more_permissive_than_default(self):
        """A single clinical term alongside tech words survives strict mode but
        not the default — strict is used where a category already vouched."""
        text = "Senior software engineer. Built dashboards for clinical teams."
        self.assertFalse(classify(text)[0])
        self.assertTrue(classify(text, strict=True)[0])

    def test_hit_count_is_deduplicated(self):
        """The score counts distinct vocabulary, so repeating one word cannot
        push a thin résumé over the threshold."""
        repeated = "patients " * 40
        self.assertLessEqual(classify(repeated)[2], 1)


if __name__ == "__main__":
    unittest.main()
