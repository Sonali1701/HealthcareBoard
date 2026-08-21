"""Travel pay calculator regression tests.

The UI is only useful if the two packages use the same contract assumptions,
tax only the taxable portion, and reject inputs that would create negative pay.
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.schemas.gsa import GSARates, PayPackageRequest
from app.services.gsa import calculate_pay_package


def rates() -> GSARates:
    return GSARates(
        city="Houston",
        state_code="TX",
        fiscal_year=2026,
        lodging=110,
        mie=68,
        weekly_lodging=770,
        weekly_mie=476,
        weekly_max_tax_free=1246,
        source="fallback",
    )


class PayPackageCalculatorTests(unittest.TestCase):
    def test_w2_and_per_diem_share_the_same_pay_pool(self):
        req = PayPackageRequest(
            bill_rate=100,
            margin_pct=20,
            burden_multiplier=1.20,
            hours_per_week=36,
            contract_weeks=13,
            city="Houston",
            state_code="tx",
        )
        result = calculate_pay_package(req, rates())

        self.assertEqual(req.state_code, "TX")
        self.assertEqual(result.breakdown["pool_per_hr"], 80)
        self.assertEqual(result.option_w2.taxable_rate, 66.67)
        self.assertEqual(result.option_perdiem.weekly_tax_free, 1246)
        self.assertGreater(
            result.option_perdiem.est_weekly_net,
            result.option_w2.est_weekly_net,
        )
        self.assertGreater(result.perdiem_advantage, 0)

    def test_daily_overrides_are_converted_to_weekly_stipends(self):
        req = PayPackageRequest(
            bill_rate=100,
            gsa_lodging_override=150,
            mie_override=75,
        )
        result = calculate_pay_package(req, rates())

        self.assertEqual(result.breakdown["weekly_lodging_stipend"], 1050)
        self.assertEqual(result.breakdown["weekly_mie_stipend"], 525)
        self.assertEqual(result.option_perdiem.weekly_tax_free, 1575)

    def test_negative_hours_and_extras_are_rejected(self):
        for field in (
            "ot_hours_per_week",
            "benefits_cost_per_hr",
            "completion_bonus",
            "travel_allowance",
            "reimbursements",
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PayPackageRequest(bill_rate=100, **{field: -1})

    def test_margin_and_benefits_cannot_consume_the_whole_bill_rate(self):
        with self.assertRaises(ValidationError):
            PayPackageRequest(
                bill_rate=50,
                margin_pct=80,
                benefits_cost_per_hr=10,
            )


if __name__ == "__main__":
    unittest.main()
