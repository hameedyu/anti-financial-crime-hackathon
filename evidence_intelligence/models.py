"""CaseFacts schema (``schema_version`` 1.0).

Facts describe; they never decide. No object here carries a verdict or a
final reason code. Every finding names the source IDs it rests on.

Evidence status vocabulary (``EvidenceStatus``):

* ``verified``            a document on file independently reconciles with the
                          transaction data (amount and at least one identity
                          field such as payee, account or reference, no conflicts)
* ``partially_supported`` a document supports part of the claim (e.g. amount
                          only, or a customer-prepared record)
* ``claimed_only``        only the customer's own statement supports it
* ``missing``             nothing on file addresses it (this is *not* evidence
                          of wrongdoing)
* ``contradicted``        a document on file conflicts with it
* ``unknown``             not assessable from the data
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"


class EvidenceStatus:
    VERIFIED = "verified"
    PARTIALLY_SUPPORTED = "partially_supported"
    CLAIMED_ONLY = "claimed_only"
    MISSING = "missing"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"
    ALL = (VERIFIED, PARTIALLY_SUPPORTED, CLAIMED_ONLY, MISSING, CONTRADICTED, UNKNOWN)


class Reliability:
    """Who produced a document - drives how far it can verify anything."""
    THIRD_PARTY = "third_party_document"         # invoices, statements, registers, letters from others
    INSTITUTION = "institution_record"           # KYC, staff notes, re-verification, device exports
    CUSTOMER_RECORD = "customer_prepared_record"  # takings books, collection reports, payment files
    CUSTOMER_STATEMENT = "customer_statement"     # chats, emails and letters from the customer
    THIRD_PARTY_DECLARATION = "third_party_declaration"  # e.g. a gift letter from the donor
    UNKNOWN = "unknown"


@dataclass
class Derivation:
    method: str                     # short machine-readable name
    description: str                # human-readable calculation
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskIndicator:
    indicator_id: str
    indicator_type: str
    rule_id: str | None
    observed_value: Any
    threshold: Any
    result: bool | None              # None = unknown
    calculation: str
    transaction_ids: list[str] = field(default_factory=list)
    supporting_ids: list[str] = field(default_factory=list)   # doc / list / review IDs
    alert_ids: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    verification_status: str = EvidenceStatus.UNKNOWN
    heuristic: bool = False


@dataclass
class FieldComparison:
    field: str
    document_value: Any
    transaction_value: Any
    outcome: str  # "match" | "partial" | "conflict" | "absent"
    note: str | None = None


@dataclass
class DocumentMatch:
    match_id: str
    doc_id: str
    doc_type: str
    reliability: str
    transaction_ids: list[str]
    match_kind: str                  # "single" | "aggregate"
    matched_fields: list[str]
    partial_fields: list[str]
    missing_fields: list[str]
    conflicting_fields: list[str]
    comparisons: list[FieldComparison]
    strength: str                    # "strong" | "moderate" | "weak" | "conflicting"
    verification_status: str
    explanation: str
    document_amount: Any = None
    document_amount_label: str | None = None


@dataclass
class ListMatchCandidate:
    entry_id: str
    alert_ids: list[str]
    transaction_ids: list[str]
    counterparty: str | None
    listed_name: str
    listed_kind: str
    listed_reason: str
    alert_matched_on: str | None
    matched_fields: list[str]
    conflicting_fields: list[str]
    unknown_fields: list[str]
    comparisons: list[dict]
    control_relationship: dict       # listed person's role at the counterparty on the payment date
    candidate_confidence: str        # see list_matching.CONFIDENCE
    supporting_ids: list[str]
    explanation: str


@dataclass
class ReviewApplicability:
    review_id: str
    rule_id: str
    reviewed_on: str
    verdict: str
    note: str
    days_before_window: int
    same_rule_as_current_alerts: bool
    current_alert_ids_same_rule: list[str]
    note_mentions: list[str]              # counterparties / references found in the note and current data
    referenced_documents_on_file: list[str]
    covers_current_activity: str          # "no" | "not_established"
    assessment: str
    reasons: list[str]


@dataclass
class AlertFacts:
    alert_id: str
    rule_id: str
    rule_name: str
    message: str
    measured: dict
    transaction_ids: list[str]
    recomputed: dict                 # independently calculated metrics
    discrepancies: list[dict]        # {"metric", "supplied", "recomputed", "explanation"}
    observations: dict               # rule-specific facts (lists, breakdowns)
    candidate_explanations: list[dict]  # {"doc_id"/"match_id", "status", "summary"}
    unresolved_requirements: list[dict]  # {"requirement", "status", "detail"}
    coverage: dict                   # how much of the alerted activity has supporting evidence
    risk_indicator_ids: list[str]


@dataclass
class CaseFacts:
    schema_version: str
    case_id: str
    institution: str
    customer_id: str
    alert_ids: list[str]
    held_payment_id: str | None
    review_window: dict
    customer_profile: dict
    transaction_summary: dict
    alert_facts: list[AlertFacts]
    risk_indicators: list[RiskIndicator]
    supporting_explanations: list[dict]
    contradicting_evidence: list[dict]
    document_facts: list[dict]
    document_matches: list[DocumentMatch]
    source_of_funds: dict
    internal_list_matches: list[ListMatchCandidate]
    previous_review_applicability: list[ReviewApplicability]
    unresolved_questions: list[dict]
    data_quality: dict
    evidence_index: dict
