"""Licence verification decides whether someone can be submitted to a client.

The dangerous failure is silent optimism: reporting "verified" when nothing was
actually checked. These tests pin the distinction between "the board said this
licence is active" and "nobody asked".
"""
from __future__ import annotations

import unittest
from datetime import date

from app.services import license_verify as lv


class ResultSemanticsTests(unittest.TestCase):
    def test_unverified_is_not_a_pass(self):
        r = lv.VerificationResult(status=lv.STATUS_UNVERIFIED)
        self.assertFalse(r.is_verified)
        self.assertFalse(r.is_placeable)

    def test_only_active_is_placeable(self):
        for status in (lv.STATUS_EXPIRED, lv.STATUS_DISCIPLINED, lv.STATUS_NOT_FOUND):
            r = lv.VerificationResult(status=status)
            self.assertTrue(r.is_verified, status)      # we got an answer
            self.assertFalse(r.is_placeable, status)    # but cannot submit
        self.assertTrue(lv.VerificationResult(status=lv.STATUS_ACTIVE).is_placeable)

    def test_a_board_saying_not_found_still_counts_as_answered(self):
        """"No such licence" is information, not a failure to reach the source."""
        self.assertTrue(lv.VerificationResult(status=lv.STATUS_NOT_FOUND).is_verified)


class DefaultProviderTests(unittest.TestCase):
    def test_no_configured_source_reports_unverified(self):
        r = lv.verify(license_type="RN", state_code="TX", license_number="123",
                      provider="unavailable")
        self.assertEqual(r.status, lv.STATUS_UNVERIFIED)
        self.assertFalse(r.is_placeable)
        self.assertIn("No verification source", r.detail)

    def test_an_unknown_provider_falls_back_rather_than_crashing(self):
        r = lv.verify(license_type="RN", state_code="TX", license_number="1",
                      provider="nursys-that-does-not-exist-yet")
        self.assertEqual(r.status, lv.STATUS_UNVERIFIED)


class ManualProviderTests(unittest.TestCase):
    def test_a_human_check_is_recorded_with_who_did_it(self):
        r = lv.verify(license_type="rn", state_code="tx", license_number="TX998",
                      first_name="Nina", last_name="Nurse", provider="manual",
                      status=lv.STATUS_ACTIVE, checked_by="rita@agency.test")
        self.assertTrue(r.is_verified)
        self.assertTrue(r.is_placeable)
        self.assertIn("rita@agency.test", r.detail)
        self.assertEqual(r.licensee_name, "Nina Nurse")

    def test_a_manual_expired_result_is_not_placeable(self):
        r = lv.verify(license_type="RN", state_code="CA", license_number="X",
                      provider="manual", status=lv.STATUS_EXPIRED)
        self.assertTrue(r.is_verified)
        self.assertFalse(r.is_placeable)

    def test_an_invented_status_is_rejected(self):
        """A caller cannot smuggle in an arbitrary status to force a pass."""
        r = lv.verify(license_type="RN", state_code="TX", license_number="X",
                      provider="manual", status="totally-fine")
        self.assertEqual(r.status, lv.STATUS_UNVERIFIED)
        self.assertFalse(r.is_placeable)

    def test_expiry_from_the_board_is_carried_back(self):
        when = date(2027, 6, 30)
        r = lv.verify(license_type="RN", state_code="TX", license_number="X",
                      provider="manual", expiry_date=when)
        self.assertEqual(r.expiry_date, when)


class ProviderPluggingTests(unittest.TestCase):
    def test_a_new_source_can_be_registered_and_used(self):
        class FakeBoard:
            name = "fake-board"

            def check(self, **kw):
                return lv.VerificationResult(
                    status=lv.STATUS_ACTIVE, source=self.name,
                    expiry_date=date(2030, 1, 1), is_compact=True)

        lv.register(FakeBoard())
        try:
            r = lv.verify(license_type="RN", state_code="TX",
                          license_number="1", provider="fake-board")
            self.assertTrue(r.is_placeable)
            self.assertTrue(r.is_compact)
        finally:
            lv._PROVIDERS.pop("fake-board", None)

    def test_a_source_that_raises_becomes_unverified_not_an_exception(self):
        """A board being down must not break the request that asked."""
        class BrokenBoard:
            name = "broken"

            def check(self, **kw):
                raise RuntimeError("board timed out")

        lv.register(BrokenBoard())
        try:
            r = lv.verify(license_type="RN", state_code="TX",
                          license_number="1", provider="broken")
            self.assertEqual(r.status, lv.STATUS_UNVERIFIED)
            self.assertIn("timed out", r.detail)
        finally:
            lv._PROVIDERS.pop("broken", None)


if __name__ == "__main__":
    unittest.main()
