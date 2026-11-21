import json
from pathlib import Path
from decide import decide_case

BASE = Path(r"C:\Users\User\Downloads\hackathon\hackathon")
INSTITUTIONS = ["bank", "emoney", "investment", "payments_fx"]
OUTPUT = BASE / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

def process_institution(name: str):
    folder = BASE / "institutions" / name
    if not folder.exists():
        print(f"  Skipping {name} – folder not found")
        return

    print(f"\nProcessing {name}...")
    cases = load_jsonl(folder / "cases.jsonl")
    alerts = load_jsonl(folder / "alerts.jsonl")
    customers = {c["customer_id"]: c for c in load_jsonl(folder / "customers.jsonl")}

    results = []
    for i, case in enumerate(cases):
        case_alerts = [a for a in alerts if a.get("alert_id") in case.get("alert_ids", [])]
        customer = customers.get(case.get("customer_id"), {})
        result = decide_case(case, case_alerts, customer, [])
        results.append(result)

        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(cases)}")

    out_path = OUTPUT / f"{name}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    print(f"  → wrote {len(results)} results to {out_path.name}")

print("Starting full run...")
for inst in INSTITUTIONS:
    process_institution(inst)

print("\nAll done.")