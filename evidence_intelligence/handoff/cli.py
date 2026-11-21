"""Diagnostic export of case facts (developer artefact, NOT the hackathon submission).

    python -m evidence_intelligence.handoff.cli --institution bank --output facts_bank.jsonl
    python -m evidence_intelligence.handoff.cli --institution C:/path/to/institutions/bank --output - --case-id <CASE_ID>

``--institution`` takes a folder path, or a bare institution name that is
looked up through ``AFC_INSTITUTIONS_DIR`` (see ``data_processing/config.py``).

Refuses to write over a final-submission file (``output/<institution>.jsonl``)
unless ``--force`` is given.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # allow "python evidence_intelligence/handoff/cli.py"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "evidence_intelligence.handoff"

from evidence_intelligence import FactExtractor, serialize_case_facts  # noqa: E402
from data_processing import CaseRepository, serialize_case_bundle  # noqa: E402
from data_processing.config import resolve_institution  # noqa: E402
from data_processing.repository import INSTITUTIONS  # noqa: E402
from .integration import build_policy_retrieval_input  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--institution", required=True, help="institution folder path, or a name such as 'bank'")
    ap.add_argument("--output", required=True, help="facts JSONL path, or '-' for stdout")
    ap.add_argument("--case-id", action="append", help="only these case IDs (repeatable)")
    ap.add_argument("--limit", type=int, default=None, help="first N cases only")
    ap.add_argument("--include-bundle", action="store_true", help="also write the normalised case bundle")
    ap.add_argument("--include-retrieval", action="store_true", help="also write the Person 3 retrieval input")
    ap.add_argument("--force", action="store_true", help="allow writing to an output/<institution>.jsonl path")
    args = ap.parse_args(argv)

    out_path = None if args.output == "-" else Path(args.output).resolve()
    if out_path and out_path.parent.name == "output" and out_path.stem in INSTITUTIONS and not args.force:
        print(f"refusing to write diagnostics to {out_path}: that is a final-submission path (use --force)", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    try:
        inst_path = resolve_institution(args.institution)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    repo = CaseRepository(inst_path)
    fx = FactExtractor()
    ids = args.case_id or [c.case_id for c in repo.cases]
    if args.limit is not None:
        ids = ids[: args.limit]
    fh = sys.stdout if out_path is None else out_path.open("w", encoding="utf-8", newline="\n")
    n = errs = 0
    try:
        for cid in ids:
            bundle = repo.get_case_bundle(cid)
            facts = fx.extract(bundle)
            rec = {"case_id": cid, "facts": serialize_case_facts(facts)}
            if args.include_bundle:
                rec["case_bundle"] = serialize_case_bundle(bundle)
            if args.include_retrieval:
                rec["policy_retrieval_input"] = build_policy_retrieval_input(bundle, facts)
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
            errs += sum(1 for i in bundle.data_quality if i.severity == "error")
    finally:
        if out_path is not None:
            fh.close()
    print(f"{repo.institution}: wrote facts for {n} cases in {time.perf_counter() - t0:.1f}s; "
          f"bundle errors={errs}; load issues={len(repo.load_issues)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
