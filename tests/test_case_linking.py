import unittest

from data_processing import CaseRepository, serialize_case_bundle
from tests import fixtures as F


class CaseLinkingTests(unittest.TestCase):
    def setUp(self):
        txs = [
            F.tx("T0", "C1", "2026-02-01T10:00:00Z", "in", "faster_payment", 100, cp="Old Payer"),  # history
            F.tx("T1", "C1", "2026-08-05T10:00:00Z", "in", "faster_payment", 5000, cp="Payer One"),
            F.tx("T2", "C1", "2026-08-06T10:00:00Z", "out", "faster_payment", 4900, cp="Payee One"),
            F.tx("T9", "C2", "2026-08-06T10:00:00Z", "in", "faster_payment", 999, cp="Other"),  # another customer
        ]
        alerts = [
            F.alert("AL1", "CS1", "C1", "TM01", {"window_days": 30}, ["T1"]),
            F.alert("AL2", "CS1", "C1", "TM02", {"window_days": 10}, ["T1", "T2"]),
            F.alert("AL3", "CS1", "C1", "TM02", {}, ["T9", "T404"]),  # foreign + missing transaction
            F.alert("AL9", "CS2", "C2", "TM01", {}, ["T9"]),
        ]
        docs = [(F.doc_row("D1", "C1", "invoice", "Invoice", "2026-08-01"), F.md("D1", "Invoice 1 from Payee One", "1 August 2026", "**Amount due:** £4,900.00")),
                (F.doc_row("D9", "C2", "invoice", "Invoice", "2026-08-01"), F.md("D9", "Invoice 9 from Other", "1 August 2026", "**Amount due:** £999.00"))]
        lst = [{"entry_id": "L1", "kind": "individual", "name": "Payee One", "date_of_birth": None, "identifiers": {},
                "address": None, "reason": "test", "added_on": "2025-01-01"},
               {"entry_id": "L2", "kind": "individual", "name": "Unrelated Person", "date_of_birth": None, "identifiers": {},
                "address": None, "reason": "test", "added_on": "2025-01-01"}]
        reviews = [{"review_id": "R1", "customer_id": "C1", "reviewed_on": "2025-06-01", "rule_id": "TM01", "verdict": "CLOSE", "note": "Closed."},
                   {"review_id": "R9", "customer_id": "C2", "reviewed_on": "2025-06-01", "rule_id": "TM01", "verdict": "CLOSE", "note": "Closed."}]
        self.root = F.make_institution(
            "payments_fx",
            cases=[F.case("CS1", "C1", ["AL1", "AL2", "AL3", "AL404"], held="T2", inst="payments_fx"),
                   F.case("CS2", "C2", ["AL9"], inst="payments_fx")],
            alerts=alerts, customers=[F.customer("C1"), F.customer("C2", name="Other Customer")], transactions=txs,
            documents=docs, reviews=reviews, internal_list=lst)
        self.repo = CaseRepository(self.root)
        self.b = self.repo.get_case_bundle("CS1")

    def tearDown(self):
        F.cleanup(self.root)

    def test_every_resolvable_alert_represented(self):
        self.assertEqual([a.alert_id for a in self.b.alerts], ["AL1", "AL2", "AL3"])
        self.assertIn(("missing_alert", "AL404"), {(i.code, i.ref_id) for i in self.b.data_quality})

    def test_transaction_dedup_keeps_alert_links(self):
        ct = {c.transaction.transaction_id: c for c in self.b.transactions}
        self.assertEqual(ct["T1"].alert_ids, ("AL1", "AL2"))
        self.assertEqual(len([c for c in self.b.transactions if c.transaction.transaction_id == "T1"]), 1)

    def test_missing_and_foreign_references_are_quality_issues(self):
        codes = {(i.code, i.ref_id) for i in self.b.data_quality}
        self.assertIn(("missing_transaction", "T404"), codes)
        self.assertIn(("transaction_customer_mismatch", "T9"), codes)
        self.assertNotIn("T9", {c.transaction.transaction_id for c in self.b.transactions})

    def test_held_payment_included(self):
        self.assertEqual(self.b.held_payment.transaction_id, "T2")
        self.assertTrue(next(c for c in self.b.transactions if c.transaction.transaction_id == "T2").is_held_payment)

    def test_history_kept_and_segmented(self):
        seg = {c.transaction.transaction_id: c.segment for c in self.b.transactions}
        self.assertEqual(seg["T0"], "pre_window")
        self.assertEqual(seg["T1"], "window")

    def test_no_cross_customer_contamination(self):
        self.assertEqual({d.doc_id for d in self.b.evidence_documents}, {"D1"})
        self.assertEqual({r.review_id for r in self.b.previous_reviews}, {"R1"})
        self.assertTrue(all(c.transaction.customer_id == "C1" for c in self.b.transactions))
        out = serialize_case_bundle(self.b)
        self.assertNotIn("Other Customer", str(out))

    def test_internal_list_not_copied_wholesale(self):
        ids = [c["entry_id"] for c in self.b.internal_list_candidates]
        self.assertEqual(ids, ["L1"])  # name equals a window counterparty; L2 is irrelevant

    def test_ids_preserved_and_schema_keys(self):
        out = serialize_case_bundle(self.b)
        for k in ("schema_version", "case_id", "institution", "customer_id", "opened_at", "review_window",
                  "held_payment_id", "customer", "alerts", "transactions", "evidence_documents", "previous_reviews",
                  "internal_list_candidates", "data_quality"):
            self.assertIn(k, out)
        self.assertEqual(out["held_payment_id"], "T2")
        self.assertEqual(set(out["data_quality"]), {"missing_references", "warnings", "errors"})

    def test_iteration_order_is_file_order(self):
        self.assertEqual([b.case_id for b in self.repo.iter_case_bundles()], ["CS1", "CS2"])


if __name__ == "__main__":
    unittest.main()
