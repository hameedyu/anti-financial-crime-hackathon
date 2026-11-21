"""Typed, immutable records for one institution's dataset.

Every record keeps ``raw`` - the source JSON object exactly as parsed (numbers
are parsed as :class:`decimal.Decimal`, so their original text is preserved) -
next to normalised, typed attributes. Monetary values are always ``Decimal``;
timestamps are timezone-aware UTC ``datetime`` objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class DeclaredProfile:
    source_of_funds: str | None
    account_purpose: str | None
    expected_countries: tuple[str, ...]
    expected_channels: tuple[str, ...]
    monthly_income_gbp: Decimal | None
    annual_turnover_gbp: Decimal | None


@dataclass(frozen=True)
class Customer:
    customer_id: str
    kind: str  # "individual" | "business"
    name: str
    date_of_birth: date | None
    occupation: str | None
    employer: str | None
    company_number: str | None
    incorporated_on: date | None
    business_activity: str | None
    address: str | None
    onboarded_on: date | None
    risk_rating: str | None
    declared: DeclaredProfile
    account_ids: tuple[str, ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    customer_id: str
    account_id: str
    booked_at: datetime
    direction: str  # "in" | "out"
    channel: str
    amount_gbp: Decimal
    amount: Decimal
    currency: str
    counterparty_name: str | None
    counterparty_account: str | None
    counterparty_country: str | None
    reference: str | None
    location: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def booked_date(self) -> date:
        return self.booked_at.date()

    @property
    def is_cash(self) -> bool:
        return self.channel.startswith("cash_")

    @property
    def is_card(self) -> bool:
        return self.channel == "card"


@dataclass(frozen=True)
class Alert:
    alert_id: str
    case_id: str
    customer_id: str
    rule_id: str
    rule_name: str
    created_at: datetime
    message: str
    measured: Mapping[str, Any]  # numbers as Decimal, strings untouched
    transaction_ids: tuple[str, ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ReviewWindow:
    start: date  # "from", inclusive
    end: date    # "to", inclusive

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class Case:
    case_id: str
    institution: str
    customer_id: str
    opened_at: datetime
    review_window: ReviewWindow
    alert_ids: tuple[str, ...]
    held_payment_id: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class EvidenceIndexEntry:
    doc_id: str
    customer_id: str
    type: str
    title: str
    dated: date | None
    file: str  # path relative to the institution folder, as given
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class MarkdownTable:
    headers: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    line_start: int


@dataclass(frozen=True)
class ParsedMarkdown:
    """Generic, type-agnostic structure of one Markdown evidence document."""

    title: str | None
    header_doc_id: str | None
    header_dated: date | None
    fields: tuple[tuple[str, str], ...]  # ordered ``**Key:** value`` pairs (duplicates kept)
    tables: tuple[MarkdownTable, ...]
    paragraphs: tuple[str, ...]          # narrative lines that are neither fields nor tables


@dataclass(frozen=True)
class EvidenceDocument:
    doc_id: str
    customer_id: str
    type: str
    title: str
    dated: date | None
    relative_path: str
    media_type: str  # "text/markdown" for the supplied dataset
    text: str | None
    parsed: ParsedMarkdown | None
    read_status: str  # "ok" | "missing_file" | "unreadable" | "unsupported_format"
    index_entry: EvidenceIndexEntry = field(repr=False, compare=False)


@dataclass(frozen=True)
class PreviousReview:
    review_id: str
    customer_id: str
    reviewed_on: date
    rule_id: str
    verdict: str
    note: str
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class InternalListEntry:
    entry_id: str
    kind: str
    name: str
    date_of_birth: date | None
    identifiers: Mapping[str, str]
    address: str | None
    reason: str
    added_on: date | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class DataQualityIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    source: str  # file or record the issue was found in
    ref_id: str | None = None


@dataclass(frozen=True)
class CaseTransaction:
    """A transaction placed in a case, with its relationship to the case."""

    transaction: Transaction
    segment: str  # "pre_window" | "window" | "post_window"
    alert_ids: tuple[str, ...]  # alerts in this case that cite the transaction
    is_held_payment: bool


@dataclass(frozen=True)
class CaseBundle:
    """Everything one case needs, scoped to the case's customer."""

    schema_version: str
    case: Case
    customer: Customer | None
    alerts: tuple[Alert, ...]
    transactions: tuple[CaseTransaction, ...]  # all of the customer's transactions, chronological
    held_payment: Transaction | None
    evidence_documents: tuple[EvidenceDocument, ...]
    previous_reviews: tuple[PreviousReview, ...]
    internal_list_candidates: tuple[Mapping[str, Any], ...]
    policy: "PolicyParameters"
    data_quality: tuple[DataQualityIssue, ...]

    # convenience views -------------------------------------------------
    @property
    def institution(self) -> str:
        return self.case.institution

    @property
    def case_id(self) -> str:
        return self.case.case_id

    def transaction_map(self) -> dict[str, Transaction]:
        return {ct.transaction.transaction_id: ct.transaction for ct in self.transactions}

    def window_transactions(self) -> list[Transaction]:
        return [ct.transaction for ct in self.transactions if ct.segment == "window"]

    def alert_transaction_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for a in self.alerts:
            for t in a.transaction_ids:
                seen.setdefault(t, None)
        return list(seen)


@dataclass(frozen=True)
class PolicyParameter:
    value: Any
    source: str  # "POLICY.md" when parsed from the file, "default" otherwise
    evidence: str | None  # text snippet the value was read from


@dataclass(frozen=True)
class PolicyParameters:
    institution: str
    rules: Mapping[str, Mapping[str, str]]  # rule_id -> {"name", "fires_when"}
    params: Mapping[str, PolicyParameter]
    high_risk_countries: tuple[str, ...]
    high_risk_as_of: str | None
    raw_text: str = field(repr=False, compare=False)

    def get(self, key: str, default: Any = None) -> Any:
        p = self.params.get(key)
        return default if p is None else p.value

    def runs(self, rule_id: str) -> bool:
        return rule_id in self.rules
