import unittest
from datetime import date
from decimal import Decimal

from evidence_intelligence import metrics as M
from data_processing.loaders import _build_transaction
from tests import fixtures as F


def T(*a, **k):
    return _build_transaction(F.tx(*a, **k) | {"amount_gbp": Decimal(str(a[5])), "amount": Decimal(str(a[5]))}, "test")


class MetricTests(unittest.TestCase):
    def test_decimal_sum_is_exact(self):
        txs = [T(f"T{i}", "C", "2026-08-01T00:00:00Z", "in", "x", "0.10") for i in range(3)]
        self.assertEqual(M.total(txs), Decimal("0.30"))

    def test_ratio_never_divides_by_zero(self):
        self.assertIsNone(M.ratio(Decimal(5), Decimal(0)))
        self.assertIsNone(M.ratio(Decimal(5), None))
        self.assertEqual(M.ratio(Decimal(1), Decimal(3), "0.01"), Decimal("0.33"))

    def test_window_inclusive(self):
        self.assertEqual(M.window_ending(date(2026, 8, 30), 30), (date(2026, 8, 1), date(2026, 8, 30)))

    def test_rapid_movement_fifo_never_exceeds_received(self):
        txs = [T("I1", "C", "2026-08-01T10:00:00Z", "in", "faster_payment", "1000", cp="A"),
               T("O1", "C", "2026-08-02T09:00:00Z", "out", "faster_payment", "800", cp="B"),
               T("O2", "C", "2026-08-02T09:30:00Z", "out", "faster_payment", "800", cp="B"),
               T("O3", "C", "2026-08-05T09:30:00Z", "out", "faster_payment", "800", cp="B"),   # outside 48h
               T("C1", "C", "2026-08-01T11:00:00Z", "out", "card", "50", cp="Shop")]            # card excluded
        r = M.rapid_movement_hours(txs, date(2026, 8, 1), date(2026, 8, 10), 48)
        self.assertEqual(r["outflows_within_hours_gbp"], Decimal("1600"))
        self.assertEqual(r["fifo_allocated_gbp"], Decimal("1000"))
        self.assertEqual(r["share_capped"], Decimal(1))
        self.assertNotIn("O3", r["outflow_ids"])
        self.assertNotIn("C1", r["outflow_ids"])

    def test_clusters(self):
        txs = [T(f"D{i}", "C", f"2026-08-0{i}T10:00:00Z", "in", "cash_deposit", "9500") for i in range(1, 5)]
        cl = M.clusters(txs, 10, 3)
        self.assertEqual(cl[0]["count"], 4)
        self.assertEqual(cl[0]["total_gbp"], Decimal("38000"))

    def test_dormancy_confirmed_vs_insufficient(self):
        txs = [T("A", "C", "2025-01-01T10:00:00Z", "in", "x", "1"), T("B", "C", "2026-01-01T10:00:00Z", "in", "x", "1")]
        self.assertEqual(M.dormancy(txs, date(2026, 1, 1), 180, date(2020, 1, 1))["status"], "confirmed_by_history")
        only = [T("B", "C", "2026-01-01T10:00:00Z", "in", "x", "1")]
        self.assertEqual(M.dormancy(only, date(2026, 1, 1), 180, date(2020, 1, 1))["status"], "insufficient_history")


if __name__ == "__main__":
    unittest.main()
