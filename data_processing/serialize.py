"""JSON-safe, canonical serialisation of bundles and facts.

Conventions:
* ``Decimal`` -> string with the exact original digits (``"12923.57"``), so no
  precision is lost; this applies to ``raw_source`` numbers too.
* ``datetime`` -> ISO-8601 UTC with ``Z``; ``date`` -> ``YYYY-MM-DD``.
* dataclasses -> dicts in field order; tuples -> lists.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .models import CaseBundle, EvidenceDocument, Transaction


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, float):  # never produced by the loaders; kept for safety
        return format(Decimal(repr(obj)), "f")
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [to_jsonable(v) for v in sorted(obj, key=str)]
    if dataclasses.is_dataclass(obj):
        if hasattr(obj, "to_dict"):
            return to_jsonable(obj.to_dict())
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if hasattr(obj, "items"):  # Mapping proxies
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """Stable text form: sorted keys, no insignificant whitespace."""
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _transaction(t: Transaction) -> dict:
    return {
        "transaction_id": t.transaction_id, "account_id": t.account_id, "booked_at": t.booked_at,
        "direction": t.direction, "channel": t.channel, "amount_gbp": t.amount_gbp,
        "amount": t.amount, "currency": t.currency, "counterparty_name": t.counterparty_name,
        "counterparty_account": t.counterparty_account, "counterparty_country": t.counterparty_country,
        "reference": t.reference, "location": t.location, "raw_source": t.raw,
    }


def _document(d: EvidenceDocument, include_text: bool) -> dict:
    p = d.parsed
    out = {
        "doc_id": d.doc_id, "type": d.type, "title": d.title, "dated": d.dated,
        "relative_path": d.relative_path, "media_type": d.media_type, "read_status": d.read_status,
        "parsed": None if p is None else {
            "title": p.title, "header_doc_id": p.header_doc_id, "header_dated": p.header_dated,
            "fields": [list(kv) for kv in p.fields],
            "tables": [{"headers": list(t.headers), "rows": [dict(r) for r in t.rows]} for t in p.tables],
            "paragraphs": list(p.paragraphs),
        },
        "extraction_method": "markdown_structure_parser" if p is not None else None,
        "raw_source": d.index_entry.raw,
    }
    if include_text:
        out["raw_text"] = d.text
    return out


def serialize_case_bundle(bundle: CaseBundle, *, include_document_text: bool = True) -> dict:
    """The normalised case schema (``schema_version`` 1.0)."""
    c, cu = bundle.case, bundle.customer
    customer = None
    if cu is not None:
        customer = {
            "customer_id": cu.customer_id,
            "identity": {
                "kind": cu.kind, "name": cu.name, "date_of_birth": cu.date_of_birth,
                "occupation": cu.occupation, "employer": cu.employer, "company_number": cu.company_number,
                "incorporated_on": cu.incorporated_on, "business_activity": cu.business_activity,
                "address": cu.address, "onboarded_on": cu.onboarded_on,
            },
            "account_ids": list(cu.account_ids),
            "risk_rating": cu.risk_rating,
            "declared_profile": dataclasses.asdict(cu.declared),
            "raw_source": cu.raw,
        }
    out = {
        "schema_version": bundle.schema_version,
        "case_id": c.case_id,
        "institution": c.institution,
        "customer_id": c.customer_id,
        "opened_at": c.opened_at,
        "review_window": {"from": c.review_window.start, "to": c.review_window.end},
        "alert_ids": list(c.alert_ids),
        "held_payment_id": c.held_payment_id,
        "customer": customer,
        "alerts": [{
            "alert_id": a.alert_id, "rule_id": a.rule_id, "rule_name": a.rule_name,
            "created_at": a.created_at, "message": a.message, "measured": dict(a.measured),
            "transaction_ids": list(a.transaction_ids), "raw_source": a.raw,
        } for a in bundle.alerts],
        "transactions": [{**_transaction(ct.transaction), "segment": ct.segment,
                          "alert_ids": list(ct.alert_ids), "is_held_payment": ct.is_held_payment}
                         for ct in bundle.transactions],
        "evidence_documents": [_document(d, include_document_text) for d in bundle.evidence_documents],
        "previous_reviews": [{**dataclasses.asdict(r) | {"raw": None}, "raw_source": r.raw} for r in bundle.previous_reviews],
        "internal_list_candidates": list(bundle.internal_list_candidates),
        "policy": {
            "institution": bundle.policy.institution,
            "rules": dict(bundle.policy.rules),
            "high_risk_countries": list(bundle.policy.high_risk_countries),
            "high_risk_as_of": bundle.policy.high_risk_as_of,
            "parameters": {k: {"value": p.value, "source": p.source, "evidence": p.evidence}
                           for k, p in sorted(bundle.policy.params.items())},
        },
        "data_quality": {
            "missing_references": [dataclasses.asdict(i) for i in bundle.data_quality
                                   if i.code.startswith("missing_")],
            "warnings": [dataclasses.asdict(i) for i in bundle.data_quality
                         if i.severity in ("warning", "info") and not i.code.startswith("missing_")],
            "errors": [dataclasses.asdict(i) for i in bundle.data_quality
                       if i.severity == "error" and not i.code.startswith("missing_")],
        },
    }
    for r in out["previous_reviews"]:
        r.pop("raw", None)
    return to_jsonable(out)
