"""Example integration: ``python -m evidence_intelligence.handoff.example <institution folder or name> [n]``.

Shows how Person 3 and Person 4 consume the APIs. Prints a compact view of
the first ``n`` cases; it decides nothing.
"""
from __future__ import annotations

import json
import sys

from data_processing.config import resolve_institution

from .integration import build_policy_retrieval_input, build_reasoning_input, process_institution


def main(institution: str, n: int = 2) -> None:
    for i, (bundle, facts) in enumerate(process_institution(resolve_institution(institution))):
        if i >= n:
            break
        retrieval = build_policy_retrieval_input(bundle, facts)      # -> Person 3
        reasoning = build_reasoning_input(bundle, facts)              # -> Person 4
        print(json.dumps({
            "case_id": retrieval["case_id"],
            "rules": retrieval["triggered_rule_ids"],
            "query_hints": retrieval["query_hints"],
            "statuses": retrieval["evidence_statuses"],
            "missing": [m["requirement"] for m in retrieval["missing_requirements"]],
            "supporting": [s["summary"] for s in reasoning["supporting_evidence"]][:3],
            "contradicting": [s["summary"] for s in reasoning["contradicting_evidence"]][:3],
            "allowed_evidence_ids": len(reasoning["allowed_evidence_ids"]),
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2)
