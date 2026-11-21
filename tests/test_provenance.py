import unittest

from evidence_intelligence import FactExtractor, serialize_case_facts
from evidence_intelligence.provenance import ProvenanceIndex, collect_ids, validate_evidence_ids
from data_processing import CaseRepository
from tests import fixtures as F


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        txs = [F.tx("T1", "C1", "2026-08-12T10:00:00Z", "out", "faster_payment", 3450, cp="Elmden Consulting Ltd")]
        doc = (F.doc_row("INV-1", "C1", "invoice", "Invoice", "2026-08-08"),
               F.md("INV-1", "Invoice EL/1 from Elmden Consulting Ltd", "8 August 2026", "**Amount due:** £3,450.00"))
        self.root = F.make_institution("bank", cases=[F.case("CS1", "C1", ["A1"])], customers=[F.customer("C1")],
                                       transactions=txs, documents=[doc],
                                       alerts=[F.alert("A1", "CS1", "C1", "TM01", {"window_days": 30}, ["T1"])])
        self.bundle = CaseRepository(self.root).get_case_bundle("CS1")
        self.facts = FactExtractor().extract(self.bundle)

    def tearDown(self):
        F.cleanup(self.root)

    def test_all_citations_resolve(self):
        self.assertEqual(self.facts.data_quality["unresolved_citations"], [])
        idx = ProvenanceIndex(self.bundle)
        cited = collect_ids(serialize_case_facts(self.facts))
        self.assertTrue(cited)
        self.assertTrue(all(idx.kind_of(i) for i in cited), cited)

    def test_evidence_validation_separates_alerts_and_unknown(self):
        r = validate_evidence_ids(["INV-1", "T1", "A1", "MADE-UP-9"], ProvenanceIndex(self.bundle))
        self.assertEqual(r["valid"], ["INV-1", "T1"])
        self.assertEqual(r["alert_ids_not_evidence"], ["A1"])
        self.assertEqual(r["unknown"], ["MADE-UP-9"])

    def test_every_match_and_indicator_names_real_ids(self):
        idx = ProvenanceIndex(self.bundle)
        for m in self.facts.document_matches:
            self.assertEqual(idx.kind_of(m.doc_id), "document")
            self.assertTrue(all(idx.kind_of(t) == "transaction" for t in m.transaction_ids))
        for i in self.facts.risk_indicators:
            self.assertTrue(all(idx.kind_of(t) for t in i.transaction_ids + i.supporting_ids + i.alert_ids))


if __name__ == "__main__":
    unittest.main()
