import json
import unittest
from decimal import Decimal

from evidence_intelligence import FactExtractor, serialize_case_facts
from data_processing import CaseRepository
from tests import fixtures as F


def run(inst, **kw):
    root = F.make_institution(inst, **kw)
    try:
        repo = CaseRepository(root)
        return FactExtractor().extract(repo.get_case_bundle(kw["cases"][0]["case_id"]))
    finally:
        F.cleanup(root)


def af(facts, rule):
    return next(a for a in facts.alert_facts if a.rule_id == rule)


class RuleFactTests(unittest.TestCase):
    def test_tm01_individual_baseline_and_own_account_rfi_signal(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "faster_payment", 9000, cp="Alex Testperson", acct="11-22-33 12345678"),
               F.tx("T2", "C1", "2026-08-11T10:00:00Z", "in", "bacs", 2000, cp="Employer Ltd")]
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1", income=2000)], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM01", {"received_gbp": 11000, "baseline_gbp": 2000, "multiple": 5.5,
                                                           "limit_multiple": 3, "window_days": 30}, ["T1", "T2"],
                                message="In the 30 days to 30 August 2026, the account received £11,000.00.")])
        a = af(f, "TM01")
        self.assertEqual(a.recomputed["received_gbp"], Decimal("11000"))
        self.assertEqual(a.recomputed["baseline_gbp"], Decimal("2000"))
        self.assertEqual(a.discrepancies, [])
        own = a.observations["own_name_inflows"]
        self.assertEqual(own["transaction_ids"], ["T1"])
        self.assertEqual(own["ownership_evidence"][0]["status"], "missing")  # name match alone is not ownership
        self.assertTrue(a.observations["own_account_policy"]["activity_at_or_above_rfi_multiple"])

    def test_tm01_own_account_verified_by_statement(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "faster_payment", 9000, cp="Alex Testperson", acct="11-22-33 12345678")]
        doc = (F.doc_row("STM-1", "C1", "bank_statement", "Statement", "2026-08-11"),
               F.md("STM-1", "Statement from Other Bank", "11 August 2026",
                    "**Account holder:** Alex Testperson\n**Bank:** Other Bank\n**Account:** 11-22-33 12345678\n\n"
                    "10 August 2026: transfer of £9,000.00 to Alex Testperson's account at another provider."))
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")], transactions=txs, documents=[doc],
                alerts=[F.alert("A1", "CS1", "C1", "TM01", {"window_days": 30}, ["T1"])])
        oe = af(f, "TM01").observations["own_name_inflows"]["ownership_evidence"][0]
        self.assertEqual(oe["status"], "verified")
        self.assertEqual(oe["doc_ids"], ["STM-1"])

    def test_tm01_payments_fx_uses_annual_volume_share(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "faster_payment", 300000, cp="Biz Ltd")]
        f = run("payments_fx", cases=[F.case("CS1", "C1", ["A1"], inst="payments_fx")],
                customers=[F.customer("C1", name="Biz Ltd", kind="business", turnover=1200000)], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM01", {"baseline_gbp": 100000, "window_days": 30}, ["T1"])])
        a = af(f, "TM01")
        self.assertEqual(a.recomputed["baseline_gbp"], Decimal("100000.00"))
        self.assertIn("annual payment volume", a.recomputed["baseline_basis"])

    def test_tm02_payments_fx_excludes_client_funding(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "faster_payment", 50000, cp="Biz Ltd"),        # own funding
               F.tx("T2", "C1", "2026-08-10T11:00:00Z", "in", "faster_payment", 3000, cp="Stranger Ltd"),    # third party
               F.tx("T3", "C1", "2026-08-11T10:00:00Z", "out", "swift", 2900, cp="Supplier SA", country="Spain")]
        f = run("payments_fx", cases=[F.case("CS1", "C1", ["A1"], inst="payments_fx")],
                customers=[F.customer("C1", name="Biz Ltd", kind="business", turnover=1200000)], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM02", {"window_days": 10}, ["T2", "T3"],
                                message="In the 10 days to 15 August 2026, the account received £3,000.00.")])
        a = af(f, "TM02")
        self.assertEqual(a.recomputed["received_gbp"], Decimal("3000"))
        self.assertEqual(a.recomputed["inflow_ids"], ["T2"])

    def test_tm03_band_is_institution_specific(self):
        txs = [F.tx(f"T{i}", "C1", f"2026-08-0{i}T10:00:00Z", "in", "cash_topup", 900, location="Shop") for i in range(1, 6)]
        f = run("emoney", cases=[F.case("CS1", "C1", ["A1"], inst="emoney")], customers=[F.customer("C1")], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM03", {"count": 5, "total_gbp": 4500, "window_days": 10},
                                [f"T{i}" for i in range(1, 6)], message="In the 10 days to 9 August 2026, there were 5 cash top-ups.")])
        a = af(f, "TM03")
        self.assertEqual(a.recomputed["band"], [Decimal("800.00"), Decimal("999.99")])
        self.assertEqual(a.recomputed["count"], 5)
        self.assertIn("not on their own evidence", a.observations["note"])

    def test_tm05_threshold_depends_on_customer_kind(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "cash_deposit", 12000, location="Branch")]
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])],
                customers=[F.customer("C1", name="Shop Ltd", kind="business", turnover=600000)], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM05", {"amount_gbp": 12000, "limit_gbp": 25000}, ["T1"])])
        a = af(f, "TM05")
        self.assertEqual(a.recomputed["limit_gbp"], Decimal("25000.00"))
        self.assertEqual(a.recomputed["at_or_above_limit_ids"], [])

    def test_tm06_uses_policy_list_only(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "out", "swift", 6000, cp="X", country="Lebanon"),
               F.tx("T2", "C1", "2026-08-11T10:00:00Z", "out", "swift", 6000, cp="Y", country="Narnia")]
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM06", {"total_gbp": 6000, "window_days": 30}, ["T1"],
                                message="In the 30 days to 30 August 2026, £6,000.00 moved to or from Lebanon.")])
        a = af(f, "TM06")
        self.assertEqual(list(a.recomputed["by_country"]), ["Lebanon"])

    def test_tm07_dormancy_status(self):
        txs = [F.tx("T0", "C1", "2025-09-01T10:00:00Z", "in", "faster_payment", 10, cp="P"),
               F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "faster_payment", 6000, cp="P")]
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM07", {"dormant_days": 343, "moved_gbp": 6000}, ["T1"])])
        a = af(f, "TM07")
        self.assertEqual(a.recomputed["dormancy"]["status"], "confirmed_by_history")
        self.assertEqual(a.recomputed["dormancy"]["longest_gap"]["gap_days"], 343)
        req = a.unresolved_requirements[0]
        self.assertEqual(req["status"], "missing")  # no re-verification record on file

    def test_tm10_pairs_purchase_and_redemption(self):
        txs = [F.tx("T1", "C1", "2026-07-01T10:00:00Z", "in", "faster_payment", 10000, cp="Alex Testperson"),
               F.tx("T2", "C1", "2026-07-02T10:00:00Z", "out", "fund_subscription", 10000, cp="Fund", ref="FUND BUY ABC"),
               F.tx("T3", "C1", "2026-08-01T10:00:00Z", "in", "fund_redemption", 9800, cp="Fund", ref="FUND SELL ABC"),
               F.tx("T4", "C1", "2026-08-02T10:00:00Z", "out", "faster_payment", 9800, cp="Someone Else")]
        f = run("investment", cases=[F.case("CS1", "C1", ["A1"], inst="investment")], customers=[F.customer("C1")], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM10", {"held_days": 30, "bought_gbp": 10000, "redeemed_gbp": 9800}, ["T2", "T3"])])
        a = af(f, "TM10")
        self.assertEqual(a.recomputed["pairs"][0]["held_days"], 30)
        self.assertEqual(a.discrepancies, [])
        self.assertEqual(a.observations["withdrawals_to_third_parties_after_redemption"], ["T4"])

    def test_facts_never_contain_a_verdict(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "cash_deposit", 12000, location="Branch")]
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM05", {"amount_gbp": 12000, "limit_gbp": 10000}, ["T1"])])
        text = json.dumps(serialize_case_facts(f))
        for word in ('"verdict"', '"reason_code"', '"CLOSE"', '"RFI"', '"ESCALATE"'):
            self.assertNotIn(word, text)

    def test_absence_of_documents_is_missing_not_suspicious(self):
        txs = [F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "cash_deposit", 12000, location="Branch")]
        f = run("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")], transactions=txs,
                alerts=[F.alert("A1", "CS1", "C1", "TM05", {"amount_gbp": 12000, "limit_gbp": 10000}, ["T1"])])
        self.assertEqual(f.source_of_funds["status"], "missing")
        self.assertIn("not that it is suspicious", f.source_of_funds["note"])
        self.assertFalse(any(i.indicator_type.startswith("document_signal") for i in f.risk_indicators))


if __name__ == "__main__":
    unittest.main()
