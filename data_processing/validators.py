"""Invariant checks on an assembled case bundle."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import DataQualityIssue

if TYPE_CHECKING:
    from .models import CaseBundle


def validate_case_bundle(bundle: "CaseBundle") -> list[DataQualityIssue]:
    """Return issues *not already recorded* during assembly.

    Checks: one customer throughout, alert/transaction links resolve inside the
    bundle, the held payment is in the bundle, documents were read, and IDs are
    unique.
    """
    issues: list[DataQualityIssue] = []
    src = f"case {bundle.case.case_id}"
    cid = bundle.case.customer_id
    recorded = {(i.code, i.ref_id) for i in bundle.data_quality}

    def add(sev, code, msg, ref=None):
        if (code, ref) not in recorded:
            issues.append(DataQualityIssue(sev, code, msg, src, ref))

    if not bundle.alerts:
        add("error", "no_alerts", "the case has no resolvable alerts")
    txids = [ct.transaction.transaction_id for ct in bundle.transactions]
    if len(txids) != len(set(txids)):
        add("error", "duplicate_transaction_in_bundle", "a transaction appears twice in the bundle")
    for ct in bundle.transactions:
        if ct.transaction.customer_id != cid:
            add("error", "foreign_transaction", f"{ct.transaction.transaction_id} belongs to another customer",
                ct.transaction.transaction_id)
    present = set(txids)
    for a in bundle.alerts:
        for t in a.transaction_ids:
            if t not in present:
                add("warning", "missing_transaction", f"alert {a.alert_id} cites {t}, not available in the bundle", t)
    if bundle.case.held_payment_id:
        if bundle.held_payment is None:
            add("error", "missing_held_payment", f"held payment {bundle.case.held_payment_id} unavailable",
                bundle.case.held_payment_id)
        else:
            if bundle.held_payment.direction != "out":
                add("warning", "held_payment_not_outgoing", "the held payment is not an outgoing payment",
                    bundle.held_payment.transaction_id)
            if not any(bundle.held_payment.transaction_id in a.transaction_ids for a in bundle.alerts):
                add("warning", "held_payment_not_cited", "no alert in the case cites the held payment",
                    bundle.held_payment.transaction_id)
    for d in bundle.evidence_documents:
        if d.customer_id != cid:
            add("error", "foreign_document", f"{d.doc_id} belongs to another customer", d.doc_id)
        if d.read_status not in ("ok", "ok_non_utf8"):
            add("warning", "document_unreadable", f"{d.doc_id} ({d.relative_path}): {d.read_status}", d.doc_id)
        elif d.parsed is not None and d.parsed.header_doc_id and d.parsed.header_doc_id != d.doc_id:
            add("warning", "document_id_mismatch",
                f"{d.relative_path} states Document ID {d.parsed.header_doc_id}, index says {d.doc_id}", d.doc_id)
        if d.dated and d.dated > bundle.case.opened_at.date():
            add("info", "document_dated_after_case_opened",
                f"{d.doc_id} is dated {d.dated.isoformat()}, after the case opened", d.doc_id)
    if not bundle.evidence_documents:
        add("warning", "no_documents_on_file", "no documents on file for the customer")
    for r in bundle.previous_reviews:
        if r.customer_id != cid:
            add("error", "foreign_review", f"{r.review_id} belongs to another customer", r.review_id)
    return issues
