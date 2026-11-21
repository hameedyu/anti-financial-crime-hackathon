"""Integration tests on the supplied development data (no outcomes are asserted)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evidence_intelligence import FactExtractor, canonical_json, serialize_case_facts
from data_processing import CaseRepository, serialize_case_bundle
from evidence_intelligence.handoff import build_policy_retrieval_input, build_reasoning_input
from tests.fixtures import INSTITUTIONS_DIR, REPO

N = int(os.environ.get("TM_TEST_CASES", "40"))
HAVE_DATA = INSTITUTIONS_DIR.is_dir()


@unittest.skipUnless(HAVE_DATA, "development data not present")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repos = {i: CaseRepository(INSTITUTIONS_DIR / i) for i in ("bank", "emoney", "investment", "payments_fx")}

    def test_all_institutions_load_without_issues(self):
        for inst, repo in self.repos.items():
            self.assertEqual(len(repo.cases), 1000, inst)
            self.assertEqual([i for i in repo.load_issues if i.severity == "error"], [], inst)

    def test_bundles_cover_every_alert_and_reference(self):
        for inst, repo in self.repos.items():
            for c in repo.cases[:N]:
                b = repo.get_case_bundle(c.case_id)
                self.assertEqual([a.alert_id for a in b.alerts], list(c.alert_ids))
                present = {ct.transaction.transaction_id for ct in b.transactions}
                for a in b.alerts:
                    self.assertTrue(set(a.transaction_ids) <= present)
                if c.held_payment_id:
                    self.assertEqual(b.held_payment.transaction_id, c.held_payment_id)
                self.assertTrue(all(d.read_status == "ok" for d in b.evidence_documents))
                self.assertEqual([i for i in b.data_quality if i.severity == "error"], [])

    def test_facts_deterministic_and_citations_resolve(self):
        for inst in self.repos:
            fresh = CaseRepository(INSTITUTIONS_DIR / inst)
            for c in self.repos[inst].cases[:N]:
                f1 = FactExtractor().extract(self.repos[inst].get_case_bundle(c.case_id))
                f2 = FactExtractor().extract(fresh.get_case_bundle(c.case_id))
                self.assertEqual(canonical_json(serialize_case_facts(f1)), canonical_json(serialize_case_facts(f2)), c.case_id)
                self.assertEqual(f1.data_quality["unresolved_citations"], [], c.case_id)
                self.assertEqual({af.alert_id for af in f1.alert_facts}, set(c.alert_ids))
                for af in f1.alert_facts:
                    self.assertTrue(af.recomputed or af.observations, (c.case_id, af.rule_id))

    def test_bundle_serialisation_is_canonical(self):
        repo = self.repos["payments_fx"]
        c = repo.cases[0]
        a = canonical_json(serialize_case_bundle(repo.get_case_bundle(c.case_id)))
        b = canonical_json(serialize_case_bundle(CaseRepository(INSTITUTIONS_DIR / "payments_fx").get_case_bundle(c.case_id)))
        self.assertEqual(a, b)

    def test_handoff_payloads(self):
        repo = self.repos["bank"]
        b = repo.get_case_bundle(repo.cases[0].case_id)
        f = FactExtractor().extract(b)
        r = build_policy_retrieval_input(b, f)
        self.assertEqual(r["institution"], "bank")
        self.assertTrue(r["triggered_rule_ids"])
        ri = build_reasoning_input(b, f)
        self.assertTrue(set(ri["allowed_evidence_ids"]))
        json.dumps(ri)  # JSON-safe


class CliTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_DATA, "development data not present")
    def test_cli_runs_from_any_directory_with_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "facts.jsonl"
            env = dict(os.environ, PYTHONPATH=str(REPO))
            r = subprocess.run([sys.executable, "-m", "evidence_intelligence.handoff.cli", "--institution",
                                str(INSTITUTIONS_DIR / "investment"), "--output", str(out), "--limit", "3"],
                               cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("facts", json.loads(lines[0]))

    def test_cli_refuses_final_submission_path(self):
        from evidence_intelligence.handoff.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "output" / "bank.jsonl"
            target.parent.mkdir()
            self.assertEqual(main(["--institution", str(INSTITUTIONS_DIR / "bank"), "--output", str(target)]), 2)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
