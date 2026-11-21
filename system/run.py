#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "policy_intelligence"))

from decide import decide_case

def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

def main():
    if len(sys.argv) != 3:
        print("Usage: python run.py <institution_folder> <output_file>")
        sys.exit(1)

    inst_folder = Path(sys.argv[1])
    out_file = Path(sys.argv[2])

    cases = load_jsonl(inst_folder / "cases.jsonl")
    alerts = load_jsonl(inst_folder / "alerts.jsonl")
    customers = {c["customer_id"]: c for c in load_jsonl(inst_folder / "customers.jsonl")}

    results = []
    for case in cases:
        case_alerts = [a for a in alerts if a.get("alert_id") in case.get("alert_ids", [])]
        customer = customers.get(case.get("customer_id"), {})
        result = decide_case(case, case_alerts, customer, [])
        results.append(result)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    print(f"Wrote {len(results)} results to {out_file}")

if __name__ == "__main__":
    main()