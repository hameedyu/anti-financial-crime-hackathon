"""Hand-off payloads for Person 3 (policy retrieval) and Person 4 (reasoning).

Nothing here decides a verdict. Typical use::

    from evidence_intelligence.handoff.integration import process_institution, build_reasoning_input
    for bundle, facts in process_institution("/abs/path/institutions/bank"):
        retrieval = build_policy_retrieval_input(bundle, facts)
        reasoning = build_reasoning_input(bundle, facts)
        ...  # Person 3 / Person 4
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, TypedDict

from evidence_intelligence import FactExtractor, serialize_case_facts
from evidence_intelligence.models import CaseFacts, EvidenceStatus
from data_processing import CaseRepository, serialize_case_bundle, to_jsonable
from data_processing.models import CaseBundle


class PolicyRetrievalInput(TypedDict):
    institution: str
    case_id: str
    triggered_rule_ids: list[str]
    rule_definitions: dict
    policy_parameters: dict
    observed_metrics: list[dict]
    evidence_statuses: dict
    missing_requirements: list[dict]
    query_hints: list[str]


class ReasoningInput(TypedDict):
    schema_version: str
    case_id: str
    institution: str
    case_bundle: dict
    facts: dict
    supporting_evidence: list[dict]
    contradicting_evidence: list[dict]
    per_alert_coverage: dict
    provenance_index: dict
    allowed_evidence_ids: list[str]


def process_institution(institution_path: str | Path) -> Iterator[tuple[CaseBundle, CaseFacts]]:
    """Load once, then yield ``(bundle, facts)`` per case in ``cases.jsonl`` order."""
    repo = CaseRepository(institution_path)
    fx = FactExtractor()
    for bundle in repo.iter_case_bundles():
        yield bundle, fx.extract(bundle)


_KEY_METRICS = ("received_gbp", "baseline_gbp", "multiple", "limit_multiple", "paid_out_gbp", "share", "invested_gbp",
                "count", "total_gbp", "distinct_counterparties", "amount_gbp", "limit_gbp", "dormant_days", "moved_gbp",
                "held_days", "flows", "candidate_confidence", "outflows_within_hours_gbp", "share_capped", "withdrawn_gbp",
                "withdrawn_share", "invested_share", "distinct_counterparties_by_name", "moved_since_reactivation_gbp")


def build_policy_retrieval_input(bundle: CaseBundle, facts: CaseFacts) -> PolicyRetrievalInput:
    rules = sorted({a.rule_id for a in bundle.alerts})
    metrics = []
    for af in facts.alert_facts:
        rec = {k: v for k, v in af.recomputed.items() if k in _KEY_METRICS}
        metrics.append({"alert_id": af.alert_id, "rule_id": af.rule_id, "measured": af.measured, "recomputed": rec,
                        "discrepancies": af.discrepancies})
    statuses = {
        "source_of_funds": facts.source_of_funds["status"],
        "per_alert_coverage": {af.alert_id: sorted(af.coverage.get("by_status", {}).keys()) for af in facts.alert_facts},
        "held_payment": [r.get("status") for r in facts.data_quality["held_payment_requirements"]],
        "internal_list": {lm.entry_id: lm.candidate_confidence for lm in facts.internal_list_matches},
        "own_account_activity_at_or_above_rfi_multiple": [
            af.observations.get("own_account_policy", {}).get("activity_at_or_above_rfi_multiple")
            for af in facts.alert_facts if af.rule_id == "TM01"],
    }
    hints = []
    for af in facts.alert_facts:
        hints.append(f"{bundle.institution} {af.rule_id} {af.rule_name}")
    if facts.data_quality["held_payment_requirements"]:
        hints.append(f"{bundle.institution} held payment invoice or contract before release")
    if any(af.rule_id == "TM22" for af in facts.alert_facts):
        hints.append(f"{bundle.institution} internal list match different party RFI escalate")
    if any(af.observations.get("own_name_inflows", {}).get("transaction_ids") for af in facts.alert_facts):
        hints.append(f"{bundle.institution} own account transfers 5 times declared income updated profile")
    if facts.previous_review_applicability:
        hints.append(f"{bundle.institution} earlier review covers only what it describes")
    return to_jsonable({
        "institution": bundle.institution, "case_id": bundle.case_id, "triggered_rule_ids": rules,
        "rule_definitions": {r: dict(bundle.policy.rules.get(r, {})) for r in rules},
        "policy_parameters": {k: p.value for k, p in sorted(bundle.policy.params.items())
                              if k.split(".")[0] in rules or not k.startswith("TM")},
        "observed_metrics": metrics, "evidence_statuses": statuses,
        "missing_requirements": facts.unresolved_questions, "query_hints": hints,
    })


def build_reasoning_input(bundle: CaseBundle, facts: CaseFacts, *, include_document_text: bool = True) -> ReasoningInput:
    f = serialize_case_facts(facts)
    cov = {}
    for af in f["alert_facts"]:
        c = dict(af["coverage"])
        c.pop("per_transaction", None)
        cov[af["alert_id"]] = {"rule_id": af["rule_id"], **c, "unresolved": af["unresolved_requirements"]}
    allowed = sorted(set(f["evidence_index"]["all_ids"]["transactions"]) | set(f["evidence_index"]["all_ids"]["documents"]) |
                     set(f["evidence_index"]["all_ids"]["list_entries"]) | set(f["evidence_index"]["all_ids"]["reviews"]))
    return {
        "schema_version": facts.schema_version, "case_id": bundle.case_id, "institution": bundle.institution,
        "case_bundle": serialize_case_bundle(bundle, include_document_text=include_document_text),
        "facts": f, "supporting_evidence": f["supporting_explanations"], "contradicting_evidence": f["contradicting_evidence"],
        "per_alert_coverage": cov, "provenance_index": f["evidence_index"], "allowed_evidence_ids": allowed,
    }


