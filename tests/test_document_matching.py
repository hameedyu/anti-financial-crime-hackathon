import unittest

from evidence_intelligence import FactExtractor
from data_processing import CaseRepository
from tests import fixtures as F


def facts(docs, txs, alerts_tids, rule="TM01", inst="bank", customer=None):
    root = F.make_institution(inst, cases=[F.case("CS1", "C1", ["A1"], inst=inst)], customers=[customer or F.customer("C1")],
                              transactions=txs, documents=docs,
                              alerts=[F.alert("A1", "CS1", "C1", rule, {"window_days": 30}, alerts_tids)])
    try:
        return FactExtractor().extract(CaseRepository(root).get_case_bundle("CS1"))
    finally:
        F.cleanup(root)


INVOICE = (F.doc_row("INV-1", "C1", "invoice", "Invoice", "2026-08-08"),
           F.md("INV-1", "Invoice EL/4296 from Elmden Consulting Ltd", "8 August 2026",
                "**Bill to:** Alex Testperson\n**Description:** Course\n**Amount due:** £3,450.00\n"
                "**Pay to:** Elmden Consulting Ltd, account 91-64-14 46112915"))


class DocumentMatchingTests(unittest.TestCase):
    def test_invoice_matching_amount_payee_account_is_verified(self):
        t = F.tx("T1", "C1", "2026-08-12T10:00:00Z", "out", "faster_payment", 3450, cp="Elmden Consulting Ltd",
                 acct="91-64-14 46112915", ref="EL/4296")
        f = facts([INVOICE], [t], ["T1"])
        m = f.document_matches[0]
        self.assertEqual(m.strength, "strong")
        self.assertEqual(m.verification_status, "verified")
        self.assertEqual(set(m.matched_fields) >= {"amount", "counterparty", "account", "reference"}, True)

    def test_amount_match_with_different_payee_is_conflicting(self):
        t = F.tx("T1", "C1", "2026-08-12T10:00:00Z", "out", "faster_payment", 3450, cp="Somebody Else Ltd",
                 acct="10-10-10 10101010")
        f = facts([INVOICE], [t], ["T1"])
        m = f.document_matches[0]
        self.assertEqual(m.strength, "conflicting")
        self.assertEqual(m.verification_status, "contradicted")
        self.assertIn("counterparty", m.conflicting_fields)
        self.assertTrue(f.contradicting_evidence)

    def test_name_only_is_not_a_link(self):
        t = F.tx("T1", "C1", "2026-08-12T10:00:00Z", "out", "faster_payment", 999, cp="Elmden Consulting Ltd")
        f = facts([INVOICE], [t], ["T1"])
        self.assertEqual(f.document_matches, [])

    def test_amount_and_name_without_date_is_moderate_not_verified(self):
        t = F.tx("T1", "C1", "2026-12-30T10:00:00Z", "out", "faster_payment", 3450, cp="Elmden Consulting Ltd")
        f = facts([INVOICE], [t], ["T1"])
        m = f.document_matches[0]
        self.assertEqual(m.strength, "moderate")
        self.assertEqual(m.verification_status, "partially_supported")

    def test_customer_statement_is_claimed_only(self):
        chat = (F.doc_row("CHT-1", "C1", "support_chat", "Chat", "2026-08-12"),
                F.md("CHT-1", "Chat with the customer", "12 August 2026",
                     "**Agent:** What is it for?\n\n**Customer:** It is £3,450.00 from selling my car to Jo Buyer."))
        t = F.tx("T1", "C1", "2026-08-12T10:00:00Z", "in", "faster_payment", 3450, cp="Jo Buyer")
        f = facts([chat], [t], ["T1"])
        m = f.document_matches[0]
        self.assertEqual(m.verification_status, "claimed_only")
        self.assertEqual(f.source_of_funds["per_inflow"][0]["status"], "claimed_only")

    def test_aggregate_invoice_total_equals_instalments(self):
        doc = (F.doc_row("INV-2", "C1", "invoice", "Invoice", "2026-08-01"),
               F.md("INV-2", "Invoice 2231 from Hartcote Trading FZE", "1 August 2026",
                    "**Bill to:** Biz Ltd\n**Goods:** produce\n**Total:** £40,000.00\n**Terms:** payable in four instalments"))
        txs = [F.tx(f"T{i}", "C1", f"2026-08-0{i + 1}T10:00:00Z", "out", "swift", 10000, cp="Hartcote Trading FZE", ref="2231")
               for i in range(1, 5)]
        f = facts([doc], txs, [t["transaction_id"] for t in txs], customer=F.customer("C1", name="Biz Ltd", kind="business", turnover=10**6))
        agg = [m for m in f.document_matches if m.match_kind == "aggregate"]
        self.assertTrue(agg)
        self.assertEqual(len(agg[0].transaction_ids), 4)
        self.assertIn("amount", agg[0].matched_fields)

    def test_cash_matched_to_collection_record_row(self):
        doc = (F.doc_row("COL-1", "C1", "cash_collection_reports", "Reports", "2026-08-18"),
               F.md("COL-1", "Machine collection reports from Park Ltd", "18 August 2026",
                    "| Site | Emptied | Notes | Coins | Total | Safe bag |\n|---|---|---|---|---|---|\n"
                    "| North car park | 9 August 2026 | £7,751.50 | £1,518.50 | £9,270.00 | SB1 |"))
        t = F.tx("T1", "C1", "2026-08-10T10:00:00Z", "in", "cash_deposit", 9270, location="Branch")
        f = facts([doc], [t], ["T1"], rule="TM03")
        m = [m for m in f.document_matches if m.document_amount == 9270][0]
        self.assertEqual(m.strength, "strong")
        self.assertEqual(m.verification_status, "partially_supported")  # customer-prepared record, not independent

    def test_fx_rounding_is_partial_but_gbp_rounding_is_not(self):
        doc = (F.doc_row("INV-3", "C1", "supplier_invoice", "Invoice", "2026-08-01"),
               F.md("INV-3", "Pro forma invoice from Supplier SA", "1 August 2026", "**Supplier:** Supplier SA\n**Amount:** £10,000.00"))
        fx = F.tx("T1", "C1", "2026-08-02T10:00:00Z", "out", "swift", 10020, cp="Supplier SA", currency="EUR", amount_ccy=11700)
        gbp = F.tx("T2", "C1", "2026-08-02T11:00:00Z", "out", "faster_payment", 10020, cp="Supplier SA")
        f = facts([doc], [fx, gbp], ["T1", "T2"], inst="payments_fx",
                  customer=F.customer("C1", name="Biz Ltd", kind="business", turnover=10**6))
        linked = {t for m in f.document_matches for t in m.transaction_ids}
        self.assertIn("T1", linked)
        self.assertNotIn("T2", linked)


if __name__ == "__main__":
    unittest.main()
