import json
from pathlib import Path
from decide import decide_case

print("=== Starting test ===")

# ←←← CHANGE THIS TO YOUR REAL PATH
BANK = Path(r"C:\Users\User\Downloads\hackathon\hackathon\institutions\bank")
# ←←←

print(f"Looking for: {BANK}")
print(f"Exists? {BANK.exists()}")

if not BANK.exists():
    print("ERROR: Path is wrong. Fix the BANK path.")
    exit(1)

def load_jsonl(path):
    print(f"Loading {path.name}...")
    with open(path, encoding="utf-8") as f:
        data = [json.loads(line) for line in f if line.strip()]
    print(f"  → {len(data)} records")
    return data

cases = load_jsonl(BANK / "cases.jsonl")
alerts = load_jsonl(BANK / "alerts.jsonl")
customers = load_jsonl(BANK / "customers.jsonl")

case = cases[0]
print(f"\nUsing case: {case.get('case_id')}")

case_alerts = [a for a in alerts if a.get("alert_id") in case.get("alert_ids", [])]
print(f"Alerts for this case: {len(case_alerts)}")

print("First alert keys:", list(case_alerts[0].keys()) if case_alerts else "no alerts")
print("First alert full:", case_alerts[0] if case_alerts else "no alerts")

customer = next((c for c in customers if c.get("customer_id") == case.get("customer_id")), None)
print(f"Customer found: {bool(customer)}")

result = decide_case(case, case_alerts, customer or {}, ["EV-1", "EV-2"])

print("\n=== RESULT ===")
print(json.dumps(result, indent=2))