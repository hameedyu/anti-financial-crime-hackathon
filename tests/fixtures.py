"""Synthetic institution folders for tests (no development-case data is used)."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from data_processing.config import REPO_ROOT as REPO, institutions_dir

# The real dataset (optional for the synthetic tests); see data_processing/config.py.
INSTITUTIONS_DIR = institutions_dir() or (REPO / "institutions")

FALLBACK_POLICY = """# Test bank: transaction monitoring SOP

| Rule | Name | Fires when |
|---|---|---|
| TM01 | Activity exceeds the expected profile | Money received over 30 days is more than 3 times the declared monthly income of an individual, or more than 2 times the declared monthly turnover of a business. |
| TM02 | Rapid movement of funds | Over 10 days, at least 80% of the money received (£2,000.00 or more) is paid out within 48 hours of arriving. Card spending is not counted as paid out. |
| TM03 | Repeated transactions below a threshold | 3 or more cash deposits, each between £9,000.00 and £9,999.99, within 10 days. |
| TM05 | Large or unexpected cash activity | One cash deposit or withdrawal of £10,000.00 or more for an individual, or £25,000.00 or more for a business. |
| TM06 | Unexpected higher-risk corridor | Payments of £5,000.00 or more within 30 days to or from a higher-risk country the customer did not declare. |
| TM07 | Dormant account reactivation | £5,000.00 or more moves after 180 days with no activity on the account. |
| TM22 | Counterparty on an internal list | A payer, recipient or company officer matches an entry on the internal list by name, address, date of birth or identifier. |

## Policy settings

- Cash reporting threshold: £10,000.00.
- Higher-risk countries as of 1 October 2026: Lebanon, Iraq, Haiti.
- When transfers between the customer's own accounts explain activity at 5 times the declared income or volume or more, send an RFI for an updated expected income or volume. Below 5 times, close.
"""


def policy_text(institution: str) -> str:
    p = INSTITUTIONS_DIR / institution / "POLICY.md"
    return p.read_text(encoding="utf-8") if p.is_file() else FALLBACK_POLICY


def tx(tid, cust, when, direction, channel, amount, cp=None, acct=None, country="United Kingdom", ref="REF",
       location=None, currency="GBP", amount_ccy=None, account="A-1"):
    return {"transaction_id": tid, "customer_id": cust, "account_id": account, "booked_at": when, "direction": direction,
            "channel": channel, "amount_gbp": amount, "counterparty_name": cp, "counterparty_account": acct,
            "counterparty_country": country, "reference": ref, "location": location, "currency": currency,
            "amount": amount if amount_ccy is None else amount_ccy}


def customer(cid, name="Alex Testperson", kind="individual", income=2000, turnover=None, countries=("United Kingdom",),
             account="A-1", onboarded="2020-01-01", dob="1990-01-01"):
    return {"kind": kind, "name": name, "date_of_birth": dob if kind == "individual" else None,
            "occupation": "Tester" if kind == "individual" else None, "employer": None,
            "company_number": "12345678" if kind == "business" else None,
            "incorporated_on": "2015-01-01" if kind == "business" else None,
            "business_activity": "Testing" if kind == "business" else None, "address": "1 Test Street, Testtown, TT1 1TT",
            "onboarded_on": onboarded, "risk_rating": "Low",
            "declared": {"source_of_funds": "Salary", "account_purpose": "Everyday banking",
                         "expected_countries": list(countries), "expected_channels": ["Card"],
                         "monthly_income_gbp": income if kind == "individual" else None,
                         "annual_turnover_gbp": turnover if kind == "business" else None},
            "customer_id": cid, "account_ids": [account]}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def make_institution(name: str = "bank", *, cases=(), alerts=(), customers=(), transactions=(), documents=(),
                     reviews=(), internal_list=(), policy: str | None = None, base: Path | None = None) -> Path:
    """Create ``<tmp>/<name>/`` with the eight standard files. ``documents`` items: (index_row, markdown_text|None)."""
    root = Path(base or tempfile.mkdtemp(prefix="tm_test_")) / name
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    (root / "POLICY.md").write_text(policy if policy is not None else policy_text(name), encoding="utf-8")
    write_jsonl(root / "cases.jsonl", list(cases))
    write_jsonl(root / "alerts.jsonl", list(alerts))
    write_jsonl(root / "customers.jsonl", list(customers))
    write_jsonl(root / "transactions.jsonl", list(transactions))
    write_jsonl(root / "previous_reviews.jsonl", list(reviews))
    write_jsonl(root / "internal_list.jsonl", list(internal_list))
    idx = []
    for row, text in documents:
        idx.append(row)
        if text is not None:
            (root / row["file"]).write_text(text, encoding="utf-8")
    write_jsonl(root / "evidence.jsonl", idx)
    return root


def cleanup(root: Path) -> None:
    shutil.rmtree(root.parent, ignore_errors=True)


def doc_row(doc_id, cust, typ, title, dated):
    return {"doc_id": doc_id, "customer_id": cust, "type": typ, "title": title, "dated": dated, "file": f"evidence/{doc_id}.md"}


def md(doc_id, title, dated_text, body):
    return f"# {title}\n\n**Document ID:** {doc_id}\n**Dated:** {dated_text}\n\n{body}\n"


def case(cid, cust, alert_ids, start="2026-08-01", end="2026-08-30", opened="2026-08-31T06:00:00Z", held=None, inst="bank"):
    return {"case_id": cid, "institution": inst, "customer_id": cust, "opened_at": opened,
            "review_window": {"from": start, "to": end}, "alert_ids": list(alert_ids), "held_payment_id": held}


def alert(aid, cid, cust, rule, measured, tids, message="Test alert.", created="2026-08-31T06:00:00Z", name="Rule"):
    return {"alert_id": aid, "case_id": cid, "customer_id": cust, "rule_name": name, "rule_id": rule,
            "created_at": created, "message": message, "measured": measured, "transaction_ids": list(tids)}
