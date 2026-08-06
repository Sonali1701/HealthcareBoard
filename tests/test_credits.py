"""Credits are the billing path, so the invariants here are the ones that cost
real money if they break: a candidate is paid for once, and a balance can never
be spent twice.

Runs against an in-memory SQLite database so it never touches production data.
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import CreditAccount, CreditTransaction, User
from app.models.enums import UserRole
from app.services import credits as svc


class CreditServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        import app.models  # noqa: F401  (registers every table)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.user = User(email="nurse@test.local", password_hash="x",
                         role=UserRole.recruiter)
        self.db.add(self.user)
        self.db.commit()
        self.uid = self.user.user_id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    # --- account creation -------------------------------------------------

    def test_account_is_created_on_first_use_with_the_signup_bonus(self):
        account = svc.get_account(self.db, self.uid)
        self.db.commit()
        self.assertEqual(account.balance, account.lifetime_granted)
        self.assertGreaterEqual(account.balance, 0)

    def test_balance_is_zero_for_an_account_that_does_not_exist(self):
        self.assertEqual(svc.balance(self.db, "no-such-user"), 0)

    # --- charging ---------------------------------------------------------

    def test_charging_reduces_the_balance_and_writes_a_ledger_row(self):
        svc.grant(self.db, self.uid, 10)
        self.db.commit()
        before = svc.balance(self.db, self.uid)
        result = svc.charge(self.db, self.uid, "reveal_contact",
                            entity_id="profile-1", idempotency_key="k1")
        self.db.commit()
        self.assertTrue(result["charged"])
        self.assertEqual(svc.balance(self.db, self.uid), before - result["cost"])
        txns = self.db.query(CreditTransaction).filter_by(idempotency_key="k1").all()
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].balance_after, svc.balance(self.db, self.uid))

    def test_the_same_candidate_is_never_charged_twice(self):
        """Revealing a contact you already paid for is free, however many times
        the button is clicked."""
        svc.grant(self.db, self.uid, 10)
        self.db.commit()
        first = svc.charge(self.db, self.uid, "reveal_contact",
                           entity_id="p1", idempotency_key="reveal:u:p1")
        self.db.commit()
        after_first = svc.balance(self.db, self.uid)
        for _ in range(3):
            again = svc.charge(self.db, self.uid, "reveal_contact",
                               entity_id="p1", idempotency_key="reveal:u:p1")
            self.db.commit()
            self.assertFalse(again["charged"])
            self.assertEqual(again["cost"], 0)
        self.assertEqual(svc.balance(self.db, self.uid), after_first)
        self.assertTrue(first["charged"])

    def test_different_candidates_are_charged_separately(self):
        svc.grant(self.db, self.uid, 10)
        self.db.commit()
        start = svc.balance(self.db, self.uid)
        for i in range(3):
            svc.charge(self.db, self.uid, "reveal_contact",
                       entity_id=f"p{i}", idempotency_key=f"reveal:u:p{i}")
            self.db.commit()
        self.assertEqual(svc.balance(self.db, self.uid), start - 3)

    def test_an_empty_balance_refuses_the_charge(self):
        account = svc.get_account(self.db, self.uid)
        account.balance = 0
        self.db.commit()
        with self.assertRaises(svc.InsufficientCredits):
            svc.charge(self.db, self.uid, "reveal_contact", idempotency_key="k")
        self.db.rollback()
        self.assertEqual(svc.balance(self.db, self.uid), 0)

    def test_a_refused_charge_writes_no_ledger_row(self):
        account = svc.get_account(self.db, self.uid)
        account.balance = 0
        self.db.commit()
        before = self.db.query(CreditTransaction).count()
        with self.assertRaises(svc.InsufficientCredits):
            svc.charge(self.db, self.uid, "reveal_contact", idempotency_key="nope")
        self.db.rollback()
        self.assertEqual(self.db.query(CreditTransaction).count(), before)

    def test_a_free_action_never_touches_the_balance(self):
        svc.grant(self.db, self.uid, 5)
        self.db.commit()
        before = svc.balance(self.db, self.uid)
        result = svc.charge(self.db, self.uid, "some_free_action", cost=0)
        self.db.commit()
        self.assertFalse(result["charged"])
        self.assertEqual(svc.balance(self.db, self.uid), before)

    def test_spending_the_last_credit_leaves_exactly_zero(self):
        account = svc.get_account(self.db, self.uid)
        account.balance = 1
        self.db.commit()
        svc.charge(self.db, self.uid, "reveal_contact", idempotency_key="last")
        self.db.commit()
        self.assertEqual(svc.balance(self.db, self.uid), 0)

    # --- grants and refunds ----------------------------------------------

    def test_grant_increases_balance_and_lifetime_total(self):
        svc.grant(self.db, self.uid, 25, note="top up")
        self.db.commit()
        account = self.db.query(CreditAccount).filter_by(user_id=self.uid).one()
        self.assertGreaterEqual(account.lifetime_granted, 25)
        self.assertGreaterEqual(account.balance, 25)

    def test_grant_rejects_a_negative_amount(self):
        with self.assertRaises(ValueError):
            svc.grant(self.db, self.uid, -5)

    def test_refund_restores_a_charge(self):
        svc.grant(self.db, self.uid, 10)
        self.db.commit()
        svc.charge(self.db, self.uid, "reveal_contact", idempotency_key="r1")
        self.db.commit()
        mid = svc.balance(self.db, self.uid)
        svc.refund(self.db, self.uid, 1, action="reveal_contact")
        self.db.commit()
        self.assertEqual(svc.balance(self.db, self.uid), mid + 1)

    # --- pricing ----------------------------------------------------------

    def test_only_revealing_a_contact_costs_anything(self):
        self.assertEqual(svc.cost_of("reveal_contact"), 1)
        self.assertEqual(svc.cost_of("outreach_email"), 0)
        self.assertEqual(svc.cost_of("anything_else"), 0)


if __name__ == "__main__":
    unittest.main()
