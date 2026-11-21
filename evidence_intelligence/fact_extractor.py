
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json


def money(value):
    """Convert an amount to Decimal without floating-point errors."""
    try:
        return Decimal(str(value).replace(",", "").replace("£", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_date(value):
    """Read an ISO date or datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_direction(value):
    value = str(value or "").lower().strip()
    if value in ("in", "incoming", "credit", "deposit"):
        return "in"
    if value in ("out", "outgoing", "debit", "withdrawal"):
        return "out"
    return None


def is_cash(transaction):
    """Detect explicit cash transactions, not all deposits."""
    kind = str(transaction.get("type") or "").lower()
    channel = str(transaction.get("channel") or "").lower()
    return "cash" in kind or "cash" in channel


def extract_facts(case):
    """
    Input: one normalized case dictionary from Person 1.
    Output: JSON-compatible facts for Person 4.

    Expected keys:
      case_id, institution, customer, alerts,
      transactions, documents, internal_list_matches
    """
    transactions = case.get("transactions") or []
    documents = case.get("documents") or []
    customer = case.get("customer") or {}
    alerts = case.get("alerts") or []

    incoming = []
    outgoing = []
    cash_transactions = []
    missing_fields = []
    invalid_transactions = []

    for index, tx in enumerate(transactions):
        amount = money(tx.get("amount"))
        direction = normalize_direction(tx.get("direction"))

        if amount is None or amount < 0 or direction is None:
            invalid_transactions.append(
                tx.get("transaction_id") or f"index:{index}"
            )
            continue

        item = {
            "transaction_id": tx.get("transaction_id"),
            "amount": amount,
            "direction": direction,
            "date": parse_date(tx.get("date")),
            "counterparty": tx.get("counterparty"),
            "country": tx.get("country"),
        }

        if direction == "in":
            incoming.append(item)
        else:
            outgoing.append(item)

        if is_cash(tx):
            cash_transactions.append(item)

    total_in = sum((t["amount"] for t in incoming), Decimal("0"))
    total_out = sum((t["amount"] for t in outgoing), Decimal("0"))

    cash_deposits = [
        t for t in cash_transactions if t["direction"] == "in"
    ]

    # An indicator, not an institution-specific policy verdict.
    large_cash = [
        t for t in cash_deposits if t["amount"] >= Decimal("10000")
    ]

    expected = money(customer.get("expected_monthly_inflow"))
    if expected is None:
        missing_fields.append("customer.expected_monthly_inflow")

    income_multiple = None
    if expected is not None and expected > 0:
        income_multiple = round(float(total_in / expected), 3)

    # Volume-based proxy. It does not prove that the same funds
    # were transferred within 48 hours.
    movement_ratio = None
    if total_in > 0:
        movement_ratio = round(
            float(min(total_out / total_in, Decimal("1"))), 3
        )

    dated_transactions = [
        t for t in incoming + outgoing if t["date"] is not None
    ]
    dated_transactions.sort(key=lambda t: t["date"])

    # Count transactions per calendar date.
    daily_counts = Counter(
        t["date"].date().isoformat() for t in dated_transactions
    )
    max_daily_count = max(daily_counts.values(), default=0)

    # Descriptive score, not a regulatory or SOP-defined score.
    velocity_score = min(100, max_daily_count * 10)

    source_documents = []
    for doc in documents:
        doc_type = str(doc.get("document_type") or "").lower()
        if any(word in doc_type for word in (
            "source_of_funds", "bank_statement",
            "payslip", "inheritance", "sale_agreement"
        )):
            source_documents.append(doc.get("document_id"))

    # No identified document is different from a verified
    # absence of adequate source-of-funds evidence.
    source_of_funds_document_identified = bool(source_documents)

    rules = sorted({
        str(alert.get("rule_id"))
        for alert in alerts
        if alert.get("rule_id")
    })

    counterparties = sorted({
        str(t["counterparty"])
        for t in incoming + outgoing
        if t["counterparty"]
    })

    facts = {
        "case_id": case.get("case_id"),
        "institution": case.get("institution"),
        "alert_rules": rules,
        "transaction_summary": {
            "transaction_count": len(incoming) + len(outgoing),
            "total_incoming_gbp": float(total_in),
            "total_outgoing_gbp": float(total_out),
            "incoming_count": len(incoming),
            "outgoing_count": len(outgoing),
            "unique_counterparty_count": len(counterparties),
            "max_transactions_per_day": max_daily_count,
        },
        "profile_comparison": {
            "expected_monthly_inflow_gbp": (
                float(expected) if expected is not None else None
            ),
            "inflow_to_expected_multiple": income_multiple,
            "review_window_comparable_to_month": None,
        },
        "risk_indicators": {
            "large_cash_deposit": bool(large_cash),
            "large_cash_transaction_ids": [
                t["transaction_id"] for t in large_cash
                if t["transaction_id"]
            ],
            "outflow_to_inflow_ratio": movement_ratio,
            "rapid_movement_48h": None,
            "velocity_score": velocity_score,
            "source_of_funds_document_identified":
                source_of_funds_document_identified,
            "source_of_funds_missing": None,
        },
        "document_ids": sorted({
            str(d["document_id"]) for d in documents
            if d.get("document_id")
        }),
        "source_document_ids": sorted({
            str(doc_id) for doc_id in source_documents if doc_id
        }),
        "missing_fields": missing_fields,
        "invalid_transactions": invalid_transactions,
        "limitations": [
            "Cash threshold is an illustrative bank threshold, "
            "not a universal institution rule.",
            "Monthly comparison requires a compatible review window.",
            "Rapid movement requires transaction-level fund-flow analysis.",
            "Document presence does not establish document adequacy.",
            "Velocity score is a descriptive heuristic, not a verdict."
        ],
    }

    return facts


if __name__ == "__main__":
    # Small example for a first local test.
    sample_case = {
        "case_id": "DEMO-001",
        "institution": "bank",
        "customer": {"expected_monthly_inflow": 5000},
        "alerts": [{"rule_id": "TM01"}, {"rule_id": "TM05"}],
        "transactions": [
            {
                "transaction_id": "TX-001",
                "amount": 12000,
                "direction": "in",
                "type": "cash_deposit",
                "date": "2026-09-01",
                "counterparty": "Customer"
            },
            {
                "transaction_id": "TX-002",
                "amount": 8000,
                "direction": "out",
                "type": "bank_transfer",
                "date": "2026-09-02",
                "counterparty": "Supplier A"
            }
        ],
        "documents": []
    }

    result = extract_facts(sample_case)
    print(json.dumps(result, indent=2))
