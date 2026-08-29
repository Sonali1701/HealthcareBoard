"""Parsing what the Quick Sourcer Hub sends back.

The Hub searches four different people-search sites and returns whichever one
answered, so the detailed `profile` block has a different shape every time. The
risk this file guards is writing junk onto a real provider's record: a masked
address that nobody can email, a phone that came back as an object rather than a
string, a value long enough to blow the column. All of that has to be caught
before it is saved, because a wrong contact looks exactly like a right one.
"""
from __future__ import annotations

import unittest

from app.services import quick_sourcer as qs

# The example response from the API documentation, trimmed to what we read.
FOUND = {
    "found": True,
    "candidate_id": 5,
    "name": "Joseph Michael Antario",
    "email": "paula1777@verizon.net",
    "phone": "(610) 217-3807",
    "address": "396 Little Creek Dr, Nazareth, PA 18064",
    "source": "familytreenow",
    "profile": {"age": 58},
    "summary": {
        "names": ["Joseph Michael Antario", "Joseph Paul Antario"],
        "phones": [
            {"number": "(610) 217-3807", "type": "Wireless", "isPrimary": True},
            {"number": "(610) 555-0100", "type": "Landline"},
        ],
        "emails": ["paula1777@verizon.net", "jantario@gmail.com"],
        "addresses": ["396 Little Creek Dr, Nazareth, PA 18064",
                      "8 N Delaware Dr, Easton, PA 18042"],
        "education": [],
        "experience": [],
    },
}

MISS = {"found": False, "candidate_id": None, "name": None, "email": None,
        "phone": None, "address": None, "source": None, "profile": None,
        "summary": None}


class ParseMatchTests(unittest.TestCase):
    def test_reads_the_documented_response(self):
        m = qs.parse_match(FOUND)
        self.assertTrue(m.found)
        self.assertEqual(m.candidate_id, 5)
        self.assertEqual(m.email, "paula1777@verizon.net")
        self.assertEqual(m.phone, "(610) 217-3807")
        self.assertEqual(m.source, "familytreenow")
        self.assertEqual(m.address, "396 Little Creek Dr, Nazareth, PA 18064")

    def test_alternatives_come_through_for_the_recruiter_to_choose_from(self):
        """The first pick is often right but not always — keep the rest."""
        m = qs.parse_match(FOUND)
        self.assertEqual(m.emails, ["paula1777@verizon.net", "jantario@gmail.com"])
        # Phones arrive as objects; only the number survives.
        self.assertEqual(m.phones, ["(610) 217-3807", "(610) 555-0100"])
        self.assertEqual(len(m.addresses), 2)

    def test_a_miss_is_empty_not_an_error(self):
        m = qs.parse_match(MISS)
        self.assertFalse(m.found)
        self.assertIsNone(m.email)
        self.assertEqual(m.emails, [])

    def test_garbage_bodies_do_not_raise(self):
        for payload in (None, [], "found", {}, {"found": True}):
            m = qs.parse_match(payload)
            self.assertIsNone(m.email, payload)


class MaskedEmailTests(unittest.TestCase):
    """searchpeoplefree screens emails for non-paying visitors: jo****1@x.com.

    Saving one would put an unusable address on the profile and make it look
    like we had found a way to reach the person. It counts as no email.
    """

    def test_masked_top_level_email_is_dropped(self):
        m = qs.parse_match({**FOUND, "email": "jo************1@yahoo.com",
                            "summary": {}})
        self.assertIsNone(m.email)

    def test_masked_summary_emails_are_dropped_but_real_ones_kept(self):
        m = qs.parse_match({
            **FOUND, "email": None,
            "summary": {"emails": ["jo****1@yahoo.com", "real@example.com"]},
        })
        self.assertEqual(m.emails, ["real@example.com"])
        self.assertEqual(m.email, "real@example.com")

    def test_a_wholly_masked_result_yields_no_email(self):
        m = qs.parse_match({**FOUND, "email": "a***@b.com",
                            "summary": {"emails": ["a***@b.com"]}})
        self.assertIsNone(m.email)


class ColumnSafetyTests(unittest.TestCase):
    """profiles.email is varchar(255) and profiles.phone varchar(30)."""

    def test_oversized_values_are_truncated_to_the_column(self):
        m = qs.parse_match({**FOUND,
                            "email": "x" * 400 + "@example.com",
                            "phone": "1" * 90,
                            "summary": {}})
        self.assertEqual(len(m.email), 255)
        self.assertEqual(len(m.phone), 30)

    def test_non_string_fields_are_ignored_rather_than_stringified(self):
        m = qs.parse_match({**FOUND, "email": {"value": "a@b.com"},
                            "phone": 6102173807, "summary": {}})
        self.assertIsNone(m.email)
        self.assertIsNone(m.phone)

    def test_a_non_integer_candidate_id_is_not_kept(self):
        """It is only useful as a key for the instant re-fetch endpoint."""
        self.assertIsNone(qs.parse_match({**FOUND, "candidate_id": "5"}).candidate_id)


class SummaryListTests(unittest.TestCase):
    def test_duplicates_are_collapsed_case_insensitively(self):
        m = qs.parse_match({**FOUND, "email": None, "summary": {
            "emails": ["A@B.com", "a@b.com", "c@d.com"]}})
        self.assertEqual(m.emails, ["A@B.com", "c@d.com"])

    def test_the_hubs_ordering_is_preserved(self):
        """The Hub puts the primary number first; that is the one we save."""
        m = qs.parse_match({**FOUND, "phone": None, "summary": {"phones": [
            {"number": "(999) 000-1111", "isPrimary": True},
            {"number": "(888) 222-3333"}]}})
        self.assertEqual(m.phone, "(999) 000-1111")


if __name__ == "__main__":
    unittest.main()
