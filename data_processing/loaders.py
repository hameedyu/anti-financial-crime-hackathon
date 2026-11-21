"""Strict JSONL loaders for the institution files.

Structural corruption (unparseable JSON, a missing required field, a wrong
type, two *different* records sharing an ID) raises :class:`IngestionError`
naming the file and line. Recoverable oddities (an exact duplicate line, an
unexpected extra field) are reported as :class:`DataQualityIssue` warnings.

Numbers are parsed with ``parse_float=Decimal`` so monetary values never pass
through binary floating point.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .document_reader import load_evidence_document
from .models import (
    Alert, Case, Customer, DataQualityIssue, DeclaredProfile, EvidenceDocument,
    EvidenceIndexEntry, InternalListEntry, PreviousReview, ReviewWindow, Transaction,
)

T = TypeVar("T")

FILES = {
    "cases": "cases.jsonl",
    "alerts": "alerts.jsonl",
    "customers": "customers.jsonl",
    "transactions": "transactions.jsonl",
    "evidence": "evidence.jsonl",
    "previous_reviews": "previous_reviews.jsonl",
    "internal_list": "internal_list.jsonl",
}


class IngestionError(Exception):
    """Raised for structural corruption that makes a file unusable."""


# --------------------------------------------------------------------------- primitives
def parse_utc_datetime(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise IngestionError(f"{where}: expected an ISO-8601 timestamp, got {value!r}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise IngestionError(f"{where}: invalid timestamp {value!r}") from exc
    if dt.tzinfo is None:
        raise IngestionError(f"{where}: timestamp {value!r} has no timezone")
    return dt.astimezone(timezone.utc)


def parse_date(value: Any, where: str, *, nullable: bool = True) -> date | None:
    if value is None:
        if nullable:
            return None
        raise IngestionError(f"{where}: date is required")
    if not isinstance(value, str):
        raise IngestionError(f"{where}: expected YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IngestionError(f"{where}: invalid date {value!r}") from exc


def to_decimal(value: Any, where: str, *, nullable: bool = True) -> Decimal | None:
    if value is None:
        if nullable:
            return None
        raise IngestionError(f"{where}: amount is required")
    if isinstance(value, bool):
        raise IngestionError(f"{where}: boolean is not an amount")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise IngestionError(f"{where}: invalid amount {value!r}") from exc
    raise IngestionError(f"{where}: invalid amount {value!r}")


def _req(obj: dict, key: str, where: str, typ: type | tuple = str) -> Any:
    if key not in obj:
        raise IngestionError(f"{where}: missing required field {key!r}")
    val = obj[key]
    if not isinstance(val, typ):
        raise IngestionError(f"{where}: field {key!r} has type {type(val).__name__}")
    return val


def _opt_str(obj: dict, key: str, where: str) -> str | None:
    val = obj.get(key)
    if val is not None and not isinstance(val, str):
        raise IngestionError(f"{where}: field {key!r} must be a string or null")
    return val


def _str_list(obj: dict, key: str, where: str) -> tuple[str, ...]:
    val = _req(obj, key, where, list)
    if not all(isinstance(v, str) for v in val):
        raise IngestionError(f"{where}: field {key!r} must be a list of strings")
    return tuple(val)


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict]]:
    """Yield ``(line_number, object)``; blank lines are skipped."""
    if not path.is_file():
        raise IngestionError(f"{path}: file not found")
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                raise IngestionError(f"{path.name}:{lineno}: invalid JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise IngestionError(f"{path.name}:{lineno}: expected a JSON object")
            yield lineno, obj


def _load(
    institution_path: Path,
    key: str,
    id_field: str,
    build: Callable[[dict, str], T],
    issues: list[DataQualityIssue] | None,
) -> list[T]:
    path = Path(institution_path) / FILES[key]
    out: list[T] = []
    seen: dict[str, dict] = {}
    for lineno, obj in iter_jsonl(path):
        where = f"{path.name}:{lineno}"
        rid = obj.get(id_field)
        if not isinstance(rid, str) or not rid:
            raise IngestionError(f"{where}: missing or invalid {id_field!r}")
        if rid in seen:
            if seen[rid] == obj:
                if issues is not None:
                    issues.append(DataQualityIssue(
                        "warning", "duplicate_record", f"exact duplicate of {rid} ignored", where, rid))
                continue
            raise IngestionError(f"{where}: conflicting duplicate {id_field} {rid!r}")
        seen[rid] = obj
        out.append(build(obj, where))
    return out


# --------------------------------------------------------------------------- builders
def _build_case(o: dict, where: str) -> Case:
    rw = _req(o, "review_window", where, dict)
    start = parse_date(rw.get("from"), f"{where} review_window.from", nullable=False)
    end = parse_date(rw.get("to"), f"{where} review_window.to", nullable=False)
    if end < start:
        raise IngestionError(f"{where}: review window ends before it starts")
    return Case(
        case_id=_req(o, "case_id", where),
        institution=_req(o, "institution", where),
        customer_id=_req(o, "customer_id", where),
        opened_at=parse_utc_datetime(o.get("opened_at"), f"{where} opened_at"),
        review_window=ReviewWindow(start, end),
        alert_ids=_str_list(o, "alert_ids", where),
        held_payment_id=_opt_str(o, "held_payment_id", where),
        raw=o,
    )


def _build_alert(o: dict, where: str) -> Alert:
    measured = _req(o, "measured", where, dict)
    norm = {k: (Decimal(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in measured.items()}
    return Alert(
        alert_id=_req(o, "alert_id", where),
        case_id=_req(o, "case_id", where),
        customer_id=_req(o, "customer_id", where),
        rule_id=_req(o, "rule_id", where),
        rule_name=_req(o, "rule_name", where),
        created_at=parse_utc_datetime(o.get("created_at"), f"{where} created_at"),
        message=_req(o, "message", where),
        measured=norm,
        transaction_ids=_str_list(o, "transaction_ids", where),
        raw=o,
    )


def _build_customer(o: dict, where: str) -> Customer:
    d = _req(o, "declared", where, dict)
    declared = DeclaredProfile(
        source_of_funds=_opt_str(d, "source_of_funds", where),
        account_purpose=_opt_str(d, "account_purpose", where),
        expected_countries=tuple(d.get("expected_countries") or ()),
        expected_channels=tuple(d.get("expected_channels") or ()),
        monthly_income_gbp=to_decimal(d.get("monthly_income_gbp"), f"{where} declared.monthly_income_gbp"),
        annual_turnover_gbp=to_decimal(d.get("annual_turnover_gbp"), f"{where} declared.annual_turnover_gbp"),
    )
    kind = _req(o, "kind", where)
    return Customer(
        customer_id=_req(o, "customer_id", where),
        kind=kind,
        name=_req(o, "name", where),
        date_of_birth=parse_date(o.get("date_of_birth"), f"{where} date_of_birth"),
        occupation=_opt_str(o, "occupation", where),
        employer=_opt_str(o, "employer", where),
        company_number=_opt_str(o, "company_number", where),
        incorporated_on=parse_date(o.get("incorporated_on"), f"{where} incorporated_on"),
        business_activity=_opt_str(o, "business_activity", where),
        address=_opt_str(o, "address", where),
        onboarded_on=parse_date(o.get("onboarded_on"), f"{where} onboarded_on"),
        risk_rating=_opt_str(o, "risk_rating", where),
        declared=declared,
        account_ids=_str_list(o, "account_ids", where),
        raw=o,
    )


def _build_transaction(o: dict, where: str) -> Transaction:
    direction = _req(o, "direction", where)
    if direction not in ("in", "out"):
        raise IngestionError(f"{where}: direction must be 'in' or 'out', got {direction!r}")
    amount_gbp = to_decimal(o.get("amount_gbp"), f"{where} amount_gbp", nullable=False)
    if amount_gbp < 0:
        raise IngestionError(f"{where}: negative amount_gbp")
    amount = to_decimal(o.get("amount"), f"{where} amount")
    return Transaction(
        transaction_id=_req(o, "transaction_id", where),
        customer_id=_req(o, "customer_id", where),
        account_id=_req(o, "account_id", where),
        booked_at=parse_utc_datetime(o.get("booked_at"), f"{where} booked_at"),
        direction=direction,
        channel=_req(o, "channel", where),
        amount_gbp=amount_gbp,
        amount=amount if amount is not None else amount_gbp,
        currency=o.get("currency") or "GBP",
        counterparty_name=_opt_str(o, "counterparty_name", where),
        counterparty_account=_opt_str(o, "counterparty_account", where),
        counterparty_country=_opt_str(o, "counterparty_country", where),
        reference=_opt_str(o, "reference", where),
        location=_opt_str(o, "location", where),
        raw=o,
    )


def _build_evidence(o: dict, where: str) -> EvidenceIndexEntry:
    return EvidenceIndexEntry(
        doc_id=_req(o, "doc_id", where),
        customer_id=_req(o, "customer_id", where),
        type=_req(o, "type", where),
        title=_opt_str(o, "title", where) or "",
        dated=parse_date(o.get("dated"), f"{where} dated"),
        file=_req(o, "file", where),
        raw=o,
    )


def _build_review(o: dict, where: str) -> PreviousReview:
    return PreviousReview(
        review_id=_req(o, "review_id", where),
        customer_id=_req(o, "customer_id", where),
        reviewed_on=parse_date(o.get("reviewed_on"), f"{where} reviewed_on", nullable=False),
        rule_id=_req(o, "rule_id", where),
        verdict=_req(o, "verdict", where),
        note=_opt_str(o, "note", where) or "",
        raw=o,
    )


def _build_list_entry(o: dict, where: str) -> InternalListEntry:
    ident = o.get("identifiers") or {}
    if not isinstance(ident, dict):
        raise IngestionError(f"{where}: identifiers must be an object")
    return InternalListEntry(
        entry_id=_req(o, "entry_id", where),
        kind=_req(o, "kind", where),
        name=_req(o, "name", where),
        date_of_birth=parse_date(o.get("date_of_birth"), f"{where} date_of_birth"),
        identifiers={str(k): str(v) for k, v in ident.items()},
        address=_opt_str(o, "address", where),
        reason=_opt_str(o, "reason", where) or "",
        added_on=parse_date(o.get("added_on"), f"{where} added_on"),
        raw=o,
    )


# --------------------------------------------------------------------------- public loaders
def load_cases(path: str | Path, issues: list | None = None) -> list[Case]:
    return _load(Path(path), "cases", "case_id", _build_case, issues)


def load_alerts(path: str | Path, issues: list | None = None) -> list[Alert]:
    return _load(Path(path), "alerts", "alert_id", _build_alert, issues)


def load_customers(path: str | Path, issues: list | None = None) -> list[Customer]:
    return _load(Path(path), "customers", "customer_id", _build_customer, issues)


def load_transactions(path: str | Path, issues: list | None = None) -> list[Transaction]:
    return _load(Path(path), "transactions", "transaction_id", _build_transaction, issues)


def load_evidence_index(path: str | Path, issues: list | None = None) -> list[EvidenceIndexEntry]:
    return _load(Path(path), "evidence", "doc_id", _build_evidence, issues)


def load_previous_reviews(path: str | Path, issues: list | None = None) -> list[PreviousReview]:
    return _load(Path(path), "previous_reviews", "review_id", _build_review, issues)


def load_internal_list(path: str | Path, issues: list | None = None) -> list[InternalListEntry]:
    return _load(Path(path), "internal_list", "entry_id", _build_list_entry, issues)


def load_documents(path: str | Path, entries: list[EvidenceIndexEntry]) -> list[EvidenceDocument]:
    """Read every Markdown document named in the evidence index."""
    root = Path(path)
    return [load_evidence_document(root, e) for e in entries]


__all__ = [
    "IngestionError", "iter_jsonl", "parse_utc_datetime", "parse_date", "to_decimal",
    "load_cases", "load_alerts", "load_customers", "load_transactions", "load_evidence_index",
    "load_previous_reviews", "load_internal_list", "load_documents", "load_evidence_document",
]
