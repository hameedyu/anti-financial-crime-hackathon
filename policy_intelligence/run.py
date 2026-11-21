#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "policy_intelligence"))

from data_processing import CaseRepository
from evidence_intelligence import FactExtractor
from decide import decide_case

def main():
    if len(sys.argv) != 3:
        print("Usage: python run.py <institution_folder> <output_file>")
        sys.exit(1)

    inst_folder = Path(sys.argv[1])
    out_file = Path(sys.argv[2])

    print(f"Loading institution from {inst_folder}...")
    repo = CaseRepository(inst_folder)
    extractor = FactExtractor()

    results = []
    count = 0
    for bundle in repo.iter_case_bundles():
        facts = extractor.extract(bundle)
        facts_dict = facts.__dict__ if hasattr(facts, "__dict__") else facts  # handle both

        # Build simple case/alerts for the function
        case = {"case_id": bundle.case_id, "alert_ids": bundle.alert_ids, "customer_id": bundle.customer_id}
        alerts = [{"alert_id": a.alert_id, "rule_id": a.rule_id, "transaction_ids": a.transaction_ids} for a in bundle.alerts]
        customer = {}

        result = decide_case(case, alerts, customer, facts=facts_dict)
        results.append(result)
        count += 1
        if count % 100 == 0:
            print(f"  processed {count}")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    print(f"Wrote {len(results)} results to {out_file}")

if __name__ == "__main__":
    main()