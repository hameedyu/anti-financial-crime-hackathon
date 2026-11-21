import json
from pathlib import Path
from decide import decide_case

BANK = Path(r"C:\Users\User\Downloads\hackathon\hackathon\institutions\bank")
OUTPUT = Path(r"C:\Users\User\Downloads\hackathon\hackathon\output")
OUTPUT.mkdir(parents=True, exist_ok=True)

def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

print("Loading data...")
cases = load_jsonl(BANK / "cases.jsonl")
alerts = load_jsonl(BANK / "alerts.jsonl")
customers = {c["customer_id"]: c for c in load_jsonl(BANK / "customers.jsonl")}

print(f"Processing {len(cases)} cases...")

results = []
for i, case in enumerate(cases):
    case_alerts = [a for a in alerts if a.get("alert_id") in case.get("alert_ids", [])]
    customer = customers.get(case.get("customer_id"), {})
    
    # Temporary: empty evidence until we wire real evidence
    evidence_ids = []
    
    result = decide_case(case, case_alerts, customer, evidence_ids)
    results.append(result)
    
    if (i + 1) % 100 == 0:
        print(f"  processed {i + 1}/{len(cases)}")

# Write the required output format
out_path = OUTPUT / "bank.jsonl"
with open(out_path, "w", encoding="utf-8") as f:
    for r in results:
        f.write(json.dumps(r) + "\n")

print(f"\nDone. Wrote {len(results)} results to {out_path}")