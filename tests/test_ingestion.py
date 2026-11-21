import json
import unittest
from datetime import timezone
from decimal import Decimal

from data_processing import CaseRepository, IngestionError, load_policy, load_transactions, serialize_case_bundle
from data_processing.document_reader import parse_markdown
from data_processing.policy import parse_policy
from tests import fixtures as F


def basic(**over):
    c = F.customer("C1")
    t1 = F.tx("T1", "C1", "2026-08-05T10:00:00Z", "in", "faster_payment", 12923.57, cp="Payer One")
    a = F.alert("AL1", "CS1", "C1", "TM01", {"received_gbp": 12923.57, "baseline_gbp": 2000, "window_days": 30}, ["T1"],
                message="In the 30 days to 30 August 2026, the account received £12,923.57.")
    kw = dict(cases=[F.case("CS1", "C1", ["AL1"])], alerts=[a], customers=[c], transactions=[t1])
    kw.update(over)
    return F.make_institution("bank", **kw)


class LoaderTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "root", None):
            F.cleanup(self.root)

    def test_decimal_and_timezone(self):
        self.root = basic()
        txs = load_transactions(self.root)
        self.assertIsInstance(txs[0].amount_gbp, Decimal)
        self.assertEqual(txs[0].amount_gbp, Decimal("12923.57"))
        self.assertEqual(txs[0].booked_at.tzinfo, timezone.utc)

    def test_raw_source_preserved_losslessly(self):
        self.root = basic()
        repo = CaseRepository(self.root)
        b = repo.get_case_bundle("CS1")
        out = serialize_case_bundle(b)
        raw = out["transactions"][0]["raw_source"]
        self.assertEqual(raw["amount_gbp"], "12923.57")  # exact original digits
        self.assertEqual(raw["transaction_id"], "T1")

    def test_invalid_json_fails_with_location(self):
        self.root = basic()
        (self.root / "transactions.jsonl").write_text('{"transaction_id": "T1",\n', encoding="utf-8")
        with self.assertRaises(IngestionError) as cm:
            load_transactions(self.root)
        self.assertIn("transactions.jsonl:1", str(cm.exception))

    def test_missing_required_field_fails(self):
        t = F.tx("T1", "C1", "2026-08-05T10:00:00Z", "in", "faster_payment", 10)
        del t["direction"]
        self.root = basic(transactions=[t])
        with self.assertRaises(IngestionError):
            load_transactions(self.root)

    def test_naive_timestamp_rejected(self):
        t = F.tx("T1", "C1", "2026-08-05T10:00:00", "in", "faster_payment", 10)
        self.root = basic(transactions=[t])
        with self.assertRaises(IngestionError):
            load_transactions(self.root)

    def test_exact_duplicate_is_warning_conflicting_duplicate_is_error(self):
        t = F.tx("T1", "C1", "2026-08-05T10:00:00Z", "in", "faster_payment", 10)
        self.root = basic(transactions=[t, dict(t)])
        issues = []
        self.assertEqual(len(load_transactions(self.root, issues)), 1)
        self.assertEqual(issues[0].code, "duplicate_record")
        F.cleanup(self.root)
        t2 = dict(t, amount_gbp=11)
        self.root = basic(transactions=[t, t2])
        with self.assertRaises(IngestionError):
            load_transactions(self.root)

    def test_missing_document_file_is_quality_issue(self):
        row = F.doc_row("DOC-1", "C1", "invoice", "Invoice", "2026-08-01")
        self.root = basic(documents=[(row, None)])
        b = CaseRepository(self.root).get_case_bundle("CS1")
        self.assertEqual(b.evidence_documents[0].read_status, "missing_file")
        self.assertIn("document_unreadable", {i.code for i in b.data_quality})

    def test_document_path_cannot_escape_institution(self):
        row = F.doc_row("DOC-1", "C1", "invoice", "Invoice", "2026-08-01")
        row["file"] = "../../outside.md"
        self.root = basic(documents=[(row, None)])
        b = CaseRepository(self.root).get_case_bundle("CS1")
        self.assertEqual(b.evidence_documents[0].read_status, "path_outside_institution")

    def test_markdown_never_reported_as_pdf(self):
        row = F.doc_row("DOC-1", "C1", "invoice", "Invoice", "2026-08-01")
        self.root = basic(documents=[(row, F.md("DOC-1", "Invoice 12 from X Ltd", "1 August 2026", "**Amount due:** £10.00"))])
        d = CaseRepository(self.root).get_case_bundle("CS1").evidence_documents[0]
        self.assertEqual(d.media_type, "text/markdown")
        self.assertEqual(d.parsed.header_doc_id, "DOC-1")


class MarkdownParserTests(unittest.TestCase):
    def test_fields_tables_paragraphs(self):
        p = parse_markdown(F.md("X-1", "Invoices from Y", "7 September 2026",
                                "**Bill to:** A Ltd\n\n| Invoice | Amount |\n|---|---|\n| INV 1 | £1,000.00 |\n\nFree text line."))
        self.assertEqual(p.title, "Invoices from Y")
        self.assertEqual(p.header_dated.isoformat(), "2026-09-07")
        self.assertIn(("Bill to", "A Ltd"), p.fields)
        self.assertEqual(p.tables[0].rows[0]["Amount"], "£1,000.00")
        self.assertEqual(p.paragraphs, ("Free text line.",))


class PolicyTests(unittest.TestCase):
    def test_each_real_policy_parses_without_defaults(self):
        for inst in ("bank", "emoney", "investment", "payments_fx"):
            path = F.INSTITUTIONS_DIR / inst
            if not path.is_dir():
                self.skipTest("dataset not present")
            pol, issues = load_policy(path)
            self.assertEqual([i for i in issues if i.code == "policy_parameter_default"], [], inst)
            self.assertIn("Lebanon", pol.high_risk_countries)

    def test_institution_specific_values(self):
        if not F.INSTITUTIONS_DIR.is_dir():
            self.skipTest("dataset not present")
        em, _ = load_policy(F.INSTITUTIONS_DIR / "emoney")
        px, _ = load_policy(F.INSTITUTIONS_DIR / "payments_fx")
        iv, _ = load_policy(F.INSTITUTIONS_DIR / "investment")
        self.assertEqual(em.get("TM03.band_low_gbp"), Decimal("800.00"))
        self.assertEqual(em.get("TM05.individual_gbp"), Decimal("1000.00"))
        self.assertTrue(px.get("TM02.excludes_client_funding"))
        self.assertEqual(px.get("TM01.baseline"), "annual_volume_monthly_share")
        self.assertEqual(px.get("invoice_required.min_gbp"), Decimal("10000"))
        self.assertEqual(iv.get("TM02.withdrawal_days"), 10)
        self.assertFalse(iv.runs("TM03"))

    def test_missing_parameter_falls_back_with_warning(self):
        pol, issues = parse_policy("x", "| TM05 | Large cash | something unparseable |\n")
        self.assertEqual(pol.params["TM05.individual_gbp"].source, "default")
        self.assertIn("policy_parameter_default", {i.code for i in issues})


if __name__ == "__main__":
    unittest.main()
