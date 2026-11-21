#!/usr/bin/env python3
"""Build a case_facts JSON from one institution case.

Usage (from decision_intelligence):
  python build_facts.py ~/Downloads/hackathon/hackathon bank 0

Writes facts.json and policy.txt next to this script.
"""

import json
import os
import sys


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def index_by(rows, key):
    out = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        out.setdefault(value, []).append(row)
    return out


def build(institution_dir, case_index):
    cases = load_jsonl(os.path.join(institution_dir, "cases.jsonl"))
    if not cases:
        raise SystemExit("no cases in " + institution_dir)
    case = cases[case_index % len(cases)]
    case_id = case.get("case_id")
    customer_id = case.get("customer_id")
    alert_ids = case.get("alert_ids") or []

    alerts = [
        row for row in load_jsonl(os.path.join(institution_dir, "alerts.jsonl"))
        if row.get("case_id") == case_id or row.get("alert_id") in alert_ids
    ]
    customers = [
        row for row in load_jsonl(os.path.join(institution_dir, "customers.jsonl"))
        if row.get("customer_id") == customer_id
    ]
    evidence_rows = [
        row for row in load_jsonl(os.path.join(institution_dir, "evidence.jsonl"))
        if row.get("customer_id") == customer_id
    ]
    reviews = [
        row for row in load_jsonl(os.path.join(institution_dir, "previous_reviews.jsonl"))
        if row.get("customer_id") == customer_id
    ]
    internal = load_jsonl(os.path.join(institution_dir, "internal_list.jsonl"))

    # Keep only transactions mentioned by the alerts, plus a small sample.
    wanted = set()
    for alert in alerts:
        for tx_id in alert.get("transaction_ids") or []:
            wanted.add(tx_id)
    transactions = []
    for row in load_jsonl(os.path.join(institution_dir, "transactions.jsonl")):
        if row.get("customer_id") == customer_id or row.get("transaction_id") in wanted:
            transactions.append(row)
            if len(transactions) >= 40:
                break

    docs = []
    evidence_dir = os.path.join(institution_dir, "evidence")
    for row in evidence_rows[:8]:
        doc_id = row.get("doc_id") or row.get("evidence_id")
        filename = row.get("file") or (doc_id + ".md" if doc_id else None)
        text = ""
        if filename:
            path = filename if os.path.isabs(filename) else os.path.join(institution_dir, filename)
            if not os.path.exists(path) and doc_id:
                path = os.path.join(evidence_dir, doc_id + ".md")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()[:1500]
        docs.append({"meta": row, "text": text})

    policy_path = os.path.join(institution_dir, "POLICY.md")
    policy = ""
    if os.path.exists(policy_path):
        with open(policy_path, "r", encoding="utf-8") as handle:
            policy = handle.read()

    facts = {
        "case_id": case_id,
        "alert_ids": alert_ids,
        "case": case,
        "customer": customers[0] if customers else None,
        "alerts": alerts,
        "transactions": transactions,
        "evidence": docs,
        "previous_reviews": reviews,
        "internal_list_sample": internal[:30],
    }
    return facts, policy


def main(argv):
    if len(argv) < 3:
        print("usage: python build_facts.py <hackathon_folder> <institution> [case_index]")
        print("example: python build_facts.py ~/Downloads/hackathon/hackathon bank 0")
        return 2
    root = os.path.expanduser(argv[1])
    institution = argv[2]
    case_index = int(argv[3]) if len(argv) > 3 else 0
    institution_dir = os.path.join(root, "institutions", institution)
    if not os.path.isdir(institution_dir):
        # Allow the user to pass the institutions path directly.
        institution_dir = os.path.join(root, institution)
    if not os.path.isdir(institution_dir):
        raise SystemExit("cannot find institution folder: " + institution_dir)

    facts, policy = build(institution_dir, case_index)
    here = os.path.dirname(os.path.abspath(__file__))
    facts_path = os.path.join(here, "facts.json")
    policy_path = os.path.join(here, "policy.txt")
    with open(facts_path, "w", encoding="utf-8") as handle:
        json.dump(facts, handle, ensure_ascii=False, indent=2)
    with open(policy_path, "w", encoding="utf-8") as handle:
        handle.write(policy)
    print("case_id:", facts.get("case_id"))
    print("alerts:", len(facts.get("alerts") or []))
    print("wrote", facts_path)
    print("wrote", policy_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
