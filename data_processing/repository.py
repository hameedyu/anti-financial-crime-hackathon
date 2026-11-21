"""CaseRepository: load one institution folder once, index it, and assemble case bundles."""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from . import loaders
from .document_reader import load_evidence_document
from .models import (
    SCHEMA_VERSION, Alert, Case, CaseBundle, CaseTransaction, Customer, DataQualityIssue,
    EvidenceDocument, EvidenceIndexEntry, InternalListEntry, PolicyParameters, PreviousReview, Transaction,
)
from .policy import load_policy
from .textutil import core_name
from .validators import validate_case_bundle

INSTITUTIONS = ("bank", "emoney", "investment", "payments_fx")


class CaseNotFound(KeyError):
    pass


class CaseRepository:
    """All records of one institution, indexed by ID.

    Loading is done once in the constructor; bundles are assembled on demand
    and contain only the case customer's own records plus the internal-list
    entries relevant to the case.
    """

    def __init__(self, institution_path: str | Path):
        self.path = Path(institution_path).resolve()
        if not self.path.is_dir():
            raise loaders.IngestionError(f"{self.path}: institution folder not found")
        self.institution = self.path.name
        self.load_issues: list[DataQualityIssue] = []
        self.policy, policy_issues = load_policy(self.path)
        self.load_issues.extend(policy_issues)

        self.cases: list[Case] = loaders.load_cases(self.path, self.load_issues)
        self.alerts: list[Alert] = loaders.load_alerts(self.path, self.load_issues)
        self.customers: list[Customer] = loaders.load_customers(self.path, self.load_issues)
        self.transactions: list[Transaction] = loaders.load_transactions(self.path, self.load_issues)
        self.evidence_index: list[EvidenceIndexEntry] = loaders.load_evidence_index(self.path, self.load_issues)
        self.previous_reviews: list[PreviousReview] = loaders.load_previous_reviews(self.path, self.load_issues)
        self.internal_list: list[InternalListEntry] = loaders.load_internal_list(self.path, self.load_issues)

        self.case_by_id = {c.case_id: c for c in self.cases}
        self.alert_by_id = {a.alert_id: a for a in self.alerts}
        self.customer_by_id = {c.customer_id: c for c in self.customers}
        self.transaction_by_id = {t.transaction_id: t for t in self.transactions}
        self.evidence_by_id = {e.doc_id: e for e in self.evidence_index}
        self.review_by_id = {r.review_id: r for r in self.previous_reviews}
        self.list_entry_by_id = {e.entry_id: e for e in self.internal_list}

        self.alerts_by_case: dict[str, list[Alert]] = defaultdict(list)
        for a in self.alerts:
            self.alerts_by_case[a.case_id].append(a)
        self.transactions_by_customer: dict[str, list[Transaction]] = defaultdict(list)
        for t in self.transactions:
            self.transactions_by_customer[t.customer_id].append(t)
        for lst in self.transactions_by_customer.values():
            lst.sort(key=lambda t: (t.booked_at, t.transaction_id))
        self.evidence_by_customer: dict[str, list[EvidenceIndexEntry]] = defaultdict(list)
        for e in self.evidence_index:
            self.evidence_by_customer[e.customer_id].append(e)
        self.reviews_by_customer: dict[str, list[PreviousReview]] = defaultdict(list)
        for r in self.previous_reviews:
            self.reviews_by_customer[r.customer_id].append(r)
        for lst in self.reviews_by_customer.values():
            lst.sort(key=lambda r: (r.reviewed_on, r.review_id))
        self.list_by_core_name: dict[str, list[InternalListEntry]] = defaultdict(list)
        for e in self.internal_list:
            self.list_by_core_name[core_name(e.name)].append(e)

        self._doc_cache: dict[str, EvidenceDocument] = {}
        if any(c.institution != self.institution for c in self.cases):
            self.load_issues.append(DataQualityIssue(
                "warning", "institution_name_mismatch",
                f"cases.jsonl names an institution other than the folder name {self.institution!r}", "cases.jsonl"))

    # ----------------------------------------------------------------- lookups
    def all_ids(self) -> dict[str, set[str]]:
        """Every citable ID in this institution, by kind (used for provenance checks)."""
        return {
            "case": set(self.case_by_id), "alert": set(self.alert_by_id),
            "customer": set(self.customer_by_id), "transaction": set(self.transaction_by_id),
            "document": set(self.evidence_by_id), "review": set(self.review_by_id),
            "list_entry": set(self.list_entry_by_id),
        }

    def get_document(self, doc_id: str) -> EvidenceDocument:
        doc = self._doc_cache.get(doc_id)
        if doc is None:
            doc = load_evidence_document(self.path, self.evidence_by_id[doc_id])
            self._doc_cache[doc_id] = doc
        return doc

    def transactions_between(self, customer_id: str, start, end) -> list[Transaction]:
        """Customer transactions with ``start <= booked_date <= end`` (dates, inclusive)."""
        txs = self.transactions_by_customer.get(customer_id, [])
        dates = [t.booked_date for t in txs]
        lo = bisect_left(dates, start)
        out = []
        for t in txs[lo:]:
            if t.booked_date > end:
                break
            out.append(t)
        return out

    # ----------------------------------------------------------------- assembly
    def get_case_bundle(self, case_id: str) -> CaseBundle:
        case = self.case_by_id.get(case_id)
        if case is None:
            raise CaseNotFound(case_id)
        issues: list[DataQualityIssue] = []
        src = f"case {case_id}"

        customer = self.customer_by_id.get(case.customer_id)
        if customer is None:
            issues.append(DataQualityIssue("error", "missing_customer",
                                           f"customer {case.customer_id} is not in customers.jsonl", src, case.customer_id))

        alerts: list[Alert] = []
        for aid in case.alert_ids:
            a = self.alert_by_id.get(aid)
            if a is None:
                issues.append(DataQualityIssue("error", "missing_alert", f"alert {aid} listed by the case is not in alerts.jsonl", src, aid))
                continue
            if a.customer_id != case.customer_id:
                issues.append(DataQualityIssue("error", "alert_customer_mismatch",
                                               f"alert {aid} belongs to customer {a.customer_id}; excluded", src, aid))
                continue
            if a.case_id != case.case_id:
                issues.append(DataQualityIssue("warning", "alert_case_mismatch",
                                               f"alert {aid} names case {a.case_id}", src, aid))
            alerts.append(a)
        for a in self.alerts_by_case.get(case.case_id, []):
            if a.alert_id not in case.alert_ids:
                issues.append(DataQualityIssue("warning", "alert_not_listed_by_case",
                                               f"alert {a.alert_id} names this case but is not in its alert_ids; not included", src, a.alert_id))

        # alert -> transaction links (kept per transaction, never duplicated)
        cited_by: dict[str, list[str]] = defaultdict(list)
        for a in alerts:
            for tid in a.transaction_ids:
                t = self.transaction_by_id.get(tid)
                if t is None:
                    issues.append(DataQualityIssue("warning", "missing_transaction",
                                                   f"alert {a.alert_id} cites transaction {tid}, which is not in transactions.jsonl", src, tid))
                    continue
                if t.customer_id != case.customer_id:
                    issues.append(DataQualityIssue("error", "transaction_customer_mismatch",
                                                   f"alert {a.alert_id} cites {tid} of customer {t.customer_id}; excluded", src, tid))
                    continue
                if a.alert_id not in cited_by[tid]:
                    cited_by[tid].append(a.alert_id)

        held = None
        if case.held_payment_id:
            held = self.transaction_by_id.get(case.held_payment_id)
            if held is None:
                issues.append(DataQualityIssue("error", "missing_held_payment",
                                               f"held payment {case.held_payment_id} is not in transactions.jsonl", src, case.held_payment_id))
            elif held.customer_id != case.customer_id:
                issues.append(DataQualityIssue("error", "held_payment_customer_mismatch",
                                               f"held payment {held.transaction_id} belongs to {held.customer_id}; excluded", src, held.transaction_id))
                held = None

        window = case.review_window
        cts: list[CaseTransaction] = []
        for t in self.transactions_by_customer.get(case.customer_id, []):
            d = t.booked_date
            seg = "pre_window" if d < window.start else ("window" if d <= window.end else "post_window")
            cts.append(CaseTransaction(t, seg, tuple(cited_by.get(t.transaction_id, ())),
                                       held is not None and t.transaction_id == held.transaction_id))

        docs = [self.get_document(e.doc_id) for e in sorted(self.evidence_by_customer.get(case.customer_id, []),
                                                              key=lambda e: (e.dated or window.start, e.doc_id))]
        reviews = tuple(self.reviews_by_customer.get(case.customer_id, []))

        bundle = CaseBundle(
            schema_version=SCHEMA_VERSION,
            case=case,
            customer=customer,
            alerts=tuple(alerts),
            transactions=tuple(cts),
            held_payment=held,
            evidence_documents=tuple(docs),
            previous_reviews=reviews,
            internal_list_candidates=tuple(self._list_candidates(case, customer, alerts, cts, docs)),
            policy=self.policy,
            data_quality=tuple(issues),
        )
        extra = validate_case_bundle(bundle)
        if extra:
            bundle = replace(bundle, data_quality=tuple(issues) + tuple(extra))
        return bundle

    def iter_case_bundles(self) -> Iterator[CaseBundle]:
        """Bundles in ``cases.jsonl`` order (deterministic)."""
        for c in self.cases:
            yield self.get_case_bundle(c.case_id)

    # ----------------------------------------------------------------- internal list
    def _list_candidates(self, case: Case, customer: Customer | None, alerts: list[Alert],
                         cts: list[CaseTransaction], docs: list[EvidenceDocument]) -> list[dict]:
        """Entries the case needs, not the whole list.

        * entries named by a TM22 alert;
        * entries whose name (legal-form suffixes removed) equals the name of a
          counterparty in the review window or of a person named in a
          company-register / control document on file.
        Detailed comparison happens in :mod:`evidence_intelligence.list_matching`.
        """
        reasons: dict[str, list[str]] = defaultdict(list)
        for a in alerts:
            eid = a.measured.get("list_entry")
            if isinstance(eid, str):
                if eid in self.list_entry_by_id:
                    reasons[eid].append(f"named by alert {a.alert_id}")
                else:
                    reasons.setdefault(f"__missing__{eid}", []).append(a.alert_id)
        for ct in cts:
            if ct.segment != "window" and not ct.alert_ids:
                continue
            nm = ct.transaction.counterparty_name
            for e in self.list_by_core_name.get(core_name(nm), []) if nm else []:
                r = f"name equals counterparty of {ct.transaction.transaction_id}"
                if r not in reasons[e.entry_id]:
                    reasons[e.entry_id].append(r)
        for d in docs:
            if d.parsed is None:
                continue
            for tbl in d.parsed.tables:
                for row in tbl.rows:
                    for col in ("Officer", "Person", "Person with significant control"):
                        nm = row.get(col)
                        for e in self.list_by_core_name.get(core_name(nm), []) if nm else []:
                            r = f"name appears in {d.doc_id} ({col.lower()})"
                            if r not in reasons[e.entry_id]:
                                reasons[e.entry_id].append(r)
        out = []
        for eid in sorted(reasons):
            if eid.startswith("__missing__"):
                continue
            out.append({"entry_id": eid, "included_because": reasons[eid], "entry": self.list_entry_by_id[eid].raw})
        return out


def load_institution(path: str | Path) -> CaseRepository:
    """Load and index one institution folder."""
    return CaseRepository(path)
