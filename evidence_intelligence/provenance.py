"""Evidence provenance: every ID a fact cites must exist in the case's own data.

Two scopes are checked:
* the *case* scope: the customer's transactions, documents, reviews, the
  case's alerts, and the internal-list entries attached to the case;
* the *institution* scope (optional): every ID in the loaded files.

``submission_evidence_ids`` are the IDs a final answer may put in its
``evidence`` field (transactions, documents, list entries, reviews). Alert
IDs are tracked for traceability but kept apart.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from data_processing.models import CaseBundle

ID_KEYS = {"transaction_id", "transaction_ids", "doc_id", "doc_ids", "entry_id", "review_id", "supporting_ids",
           "inflow_ids", "outflow_ids", "deposit_ids", "withdrawal_ids", "investment_ids", "conflicting_doc_ids",
           "since_reactivation_ids", "qualifying_transaction_ids", "inflow_transaction_ids", "over_limit_ids",
           "at_or_above_limit_ids", "cash_transaction_ids", "third_party_inflow_ids", "outflows_to_own_name",
           "last_activity_id", "reactivation_id", "purchase_id", "inflow_id", "outflow_id", "deposit_ids",
           "non_card_transaction_ids", "withdrawals_to_third_parties_after_redemption", "alerted_purchases",
           "reactivation_counterparties_own_name", "documents_describing_cash_payment", "non_cash_alerted",
           "same_kind_above_band_in_window", "amounts_are_round", "outflows_within_10_days_of_third_party_inflow",
           "referenced_documents_on_file", "matched_transaction_ids", "control_doc_ids"}


class ProvenanceIndex:
    def __init__(self, bundle: CaseBundle):
        self.case_id = bundle.case.case_id
        self.transactions = {ct.transaction.transaction_id for ct in bundle.transactions}
        self.documents = {d.doc_id for d in bundle.evidence_documents}
        self.reviews = {r.review_id for r in bundle.previous_reviews}
        self.list_entries = {c["entry_id"] for c in bundle.internal_list_candidates}
        self.alerts = {a.alert_id for a in bundle.alerts}

    def kind_of(self, i: str) -> str | None:
        for kind, s in (("transaction", self.transactions), ("document", self.documents), ("review", self.reviews),
                        ("list_entry", self.list_entries), ("alert", self.alerts)):
            if i in s:
                return kind
        return None

    def as_dict(self) -> dict:
        return {"transactions": sorted(self.transactions), "documents": sorted(self.documents),
                "reviews": sorted(self.reviews), "list_entries": sorted(self.list_entries), "alerts": sorted(self.alerts)}

    def submission_evidence_ids(self) -> set[str]:
        return self.transactions | self.documents | self.reviews | self.list_entries


def collect_ids(obj: Any, out: set[str] | None = None, key: str | None = None) -> set[str]:
    """IDs cited under known ID keys anywhere in a (serialised) fact structure."""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ID_KEYS:
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, str):
                        out.add(x)
                    elif isinstance(x, list):
                        out.update(i for i in x if isinstance(i, str))
            else:
                collect_ids(v, out, k)
    elif isinstance(obj, list):
        for v in obj:
            collect_ids(v, out, key)
    return out


def unresolved_citations(facts_json: dict, index: ProvenanceIndex) -> list[str]:
    """Cited IDs that do not exist in the case scope (should always be empty)."""
    return sorted(i for i in collect_ids(facts_json) if index.kind_of(i) is None)


def validate_evidence_ids(ids: Iterable[str], index: ProvenanceIndex) -> dict:
    """For Person 4: split proposed submission evidence into valid / alert / unknown."""
    valid, alerts, unknown = [], [], []
    for i in ids:
        k = index.kind_of(i)
        (alerts if k == "alert" else valid if k else unknown).append(i)
    return {"valid": valid, "alert_ids_not_evidence": alerts, "unknown": unknown}
