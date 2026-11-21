"""Read the policy settings that fact extraction needs from an institution's POLICY.md.

POLICY.md is the source of truth. Every value is parsed from the file with a
targeted pattern and stored with the snippet it came from. A value the
patterns cannot find falls back to a documented default and the bundle gets
a ``policy_parameter_default`` warning, so a changed policy is never silently
ignored.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from .models import DataQualityIssue, PolicyParameter, PolicyParameters

_MONEY = r"£([\d,]+(?:\.\d+)?)"


def _money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


# Defaults mirror the four supplied POLICY.md files; they are used only when a
# pattern fails, and each use is reported.
_DEFAULTS: dict[str, object] = {
    "TM01.individual_multiple": Decimal(3),
    "TM01.business_multiple": Decimal(2),
    "TM01.baseline": "declared_monthly",       # or "annual_volume_monthly_share"
    "TM02.window_days": 10,
    "TM02.min_share": Decimal("0.80"),
    "TM02.min_received_gbp": Decimal("2000.00"),
    "TM02.payout_hours": 48,                   # bank/e-money/payments
    "TM02.withdrawal_days": None,              # investment
    "TM02.max_invested_share": None,           # investment
    "TM02.excludes_client_funding": False,     # payments and FX
    "TM02.excludes_card": True,
    "TM03.min_count": 3,
    "TM03.band_low_gbp": Decimal("9000.00"),
    "TM03.band_high_gbp": Decimal("9999.99"),
    "TM03.window_days": 10,
    "TM03.subject": "cash_deposit",            # "cash_topup" | "payment_same_beneficiary"
    "TM04.max_counterparties": 6,
    "TM04.window_days": 30,
    "TM04.scope": "payers_or_payees",          # or "beneficiaries"
    "TM05.individual_gbp": Decimal("10000.00"),
    "TM05.business_gbp": Decimal("25000.00"),
    "TM06.min_total_gbp": Decimal("5000.00"),
    "TM06.window_days": 30,
    "TM07.min_moved_gbp": Decimal("5000.00"),
    "TM07.dormant_days": 180,
    "TM08.min_gbp": Decimal("1000.00"),
    "TM10.max_held_days": 90,
    "own_account.rfi_multiple": Decimal(5),
    "invoice_required.min_gbp": None,          # payments and FX only
    "cash_reporting_threshold_gbp": Decimal("10000.00"),
}


def _rule_rows(text: str) -> dict[str, dict[str, str]]:
    rules = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*(TM\d+)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            rules[m.group(1)] = {"name": m.group(2), "fires_when": m.group(3)}
    return rules


def parse_policy(institution: str, text: str) -> tuple[PolicyParameters, list[DataQualityIssue]]:
    rules = _rule_rows(text)
    params: dict[str, PolicyParameter] = {}

    def put(key: str, value, snippet: str | None):
        params[key] = PolicyParameter(value, "POLICY.md", snippet)

    def rule_text(rid: str) -> str:
        return rules.get(rid, {}).get("fires_when", "")

    t = rule_text("TM01")
    if m := re.search(r"more than (\d+(?:\.\d+)?) times the declared monthly income of an individual", t):
        put("TM01.individual_multiple", Decimal(m.group(1)), m.group(0))
    if m := re.search(r"more than (\d+(?:\.\d+)?) times the declared monthly turnover of a business", t):
        put("TM01.business_multiple", Decimal(m.group(1)), m.group(0))
    if m := re.search(r"more than (\d+(?:\.\d+)?) times the monthly share of the expected annual payment volume", t):
        put("TM01.individual_multiple", Decimal(m.group(1)), m.group(0))
        put("TM01.business_multiple", Decimal(m.group(1)), m.group(0))
        put("TM01.baseline", "annual_volume_monthly_share", m.group(0))
    elif "TM01" in rules:
        put("TM01.baseline", "declared_monthly", t)

    t = rule_text("TM02")
    if m := re.search(r"Over (\d+) days, at least (\d+)% of the money (?:received|deposited)[^(]*\(" + _MONEY + r" or more\)", t):
        put("TM02.window_days", int(m.group(1)), m.group(0))
        put("TM02.min_share", Decimal(m.group(2)) / 100, m.group(0))
        put("TM02.min_received_gbp", _money(m.group(3)), m.group(0))
    if m := re.search(r"paid out within (\d+) hours", t):
        put("TM02.payout_hours", int(m.group(1)), m.group(0))
        put("TM02.withdrawal_days", None, m.group(0))
    if m := re.search(r"withdrawn within (\d+) days", t):
        put("TM02.withdrawal_days", int(m.group(1)), m.group(0))
        put("TM02.payout_hours", None, m.group(0))
    if m := re.search(r"less than (\d+)% of it was invested", t):
        put("TM02.max_invested_share", Decimal(m.group(1)) / 100, m.group(0))
    if m := re.search(r"from anyone other than the client", t):
        put("TM02.excludes_client_funding", True, m.group(0))
    if m := re.search(r"Card spending is not counted as paid out", t):
        put("TM02.excludes_card", True, m.group(0))

    t = rule_text("TM03")
    if m := re.search(r"(\d+) or more (cash deposits|cash top-ups|payments to the same beneficiary), each between "
                      + _MONEY + r" and " + _MONEY + r", within (\d+) days", t):
        put("TM03.min_count", int(m.group(1)), m.group(0))
        put("TM03.subject", {"cash deposits": "cash_deposit", "cash top-ups": "cash_topup",
                             "payments to the same beneficiary": "payment_same_beneficiary"}[m.group(2)], m.group(0))
        put("TM03.band_low_gbp", _money(m.group(3)), m.group(0))
        put("TM03.band_high_gbp", _money(m.group(4)), m.group(0))
        put("TM03.window_days", int(m.group(5)), m.group(0))

    t = rule_text("TM04")
    if m := re.search(r"More than (\d+) different people or companies pay the account[^,]*, or are paid by it, within (\d+) days", t):
        put("TM04.max_counterparties", int(m.group(1)), m.group(0))
        put("TM04.window_days", int(m.group(2)), m.group(0))
        put("TM04.scope", "payers_or_payees", m.group(0))
    elif m := re.search(r"pays more than (\d+) different beneficiaries within (\d+) days", t):
        put("TM04.max_counterparties", int(m.group(1)), m.group(0))
        put("TM04.window_days", int(m.group(2)), m.group(0))
        put("TM04.scope", "beneficiaries", m.group(0))

    t = rule_text("TM05")
    if m := re.search(_MONEY + r" or more for an individual, or " + _MONEY + r" or more for a business", t):
        put("TM05.individual_gbp", _money(m.group(1)), m.group(0))
        put("TM05.business_gbp", _money(m.group(2)), m.group(0))

    t = rule_text("TM06")
    if m := re.search(r"Payments of " + _MONEY + r" or more within (\d+) days", t):
        put("TM06.min_total_gbp", _money(m.group(1)), m.group(0))
        put("TM06.window_days", int(m.group(2)), m.group(0))

    t = rule_text("TM07")
    if m := re.search(_MONEY + r" or more moves after (\d+) days", t):
        put("TM07.min_moved_gbp", _money(m.group(1)), m.group(0))
        put("TM07.dormant_days", int(m.group(2)), m.group(0))

    t = rule_text("TM08")
    if m := re.search(r"of " + _MONEY + r" or more", t):
        put("TM08.min_gbp", _money(m.group(1)), m.group(0))

    t = rule_text("TM10")
    if m := re.search(r"within (\d+) days of being bought", t):
        put("TM10.max_held_days", int(m.group(1)), m.group(0))

    if m := re.search(r"explain activity at (\d+) times the declared income or volume or more", text):
        put("own_account.rfi_multiple", Decimal(m.group(1)), m.group(0))
    if m := re.search(r"Payments of " + _MONEY + r" or more to anyone other than the client need an invoice or contract", text):
        put("invoice_required.min_gbp", _money(m.group(1)), m.group(0))
    elif "TM01" in rules:
        put("invoice_required.min_gbp", None, "no invoice-before-release requirement in POLICY.md")
    if m := re.search(r"Cash reporting threshold: " + _MONEY, text):
        put("cash_reporting_threshold_gbp", _money(m.group(1)), m.group(0))

    # Conditions that a policy expresses by *not* stating them: record the absence explicitly.
    if "TM02" in rules:
        for key, absent in (("TM02.excludes_client_funding", False), ("TM02.max_invested_share", None),
                            ("TM02.withdrawal_days", None), ("TM02.excludes_card", False)):
            params.setdefault(key, PolicyParameter(absent, "POLICY.md", "condition not stated in TM02"))
        if params["TM02.withdrawal_days"].value is not None:
            params.setdefault("TM02.payout_hours", PolicyParameter(None, "POLICY.md", "TM02 uses a withdrawal window"))

    high_risk: tuple[str, ...] = ()
    as_of = None
    issues: list[DataQualityIssue] = []
    if m := re.search(r"Higher-risk countries as of ([^:]+):\s*(.+?)\.\s*$", text, re.MULTILINE):
        as_of = m.group(1).strip()
        high_risk = tuple(c.strip() for c in m.group(2).split(",") if c.strip())
    else:
        issues.append(DataQualityIssue("warning", "policy_high_risk_list_missing",
                                       "No higher-risk country list found in POLICY.md; TM06 facts cannot classify countries.",
                                       "POLICY.md"))

    for key, default in _DEFAULTS.items():
        if key not in params:
            rid = key.split(".")[0]
            if rid.startswith("TM") and rid not in rules:
                continue  # rule does not run here: no parameter needed
            params[key] = PolicyParameter(default, "default", None)
            issues.append(DataQualityIssue("warning", "policy_parameter_default",
                                           f"{key} not found in POLICY.md; using default {default!r}", "POLICY.md", key))
    return PolicyParameters(institution, rules, params, high_risk, as_of, text), issues


def load_policy(institution_path: str | Path) -> tuple[PolicyParameters, list[DataQualityIssue]]:
    p = Path(institution_path)
    path = p / "POLICY.md"
    if not path.is_file():
        from .loaders import IngestionError
        raise IngestionError(f"{path}: POLICY.md not found")
    return parse_policy(p.name, path.read_text(encoding="utf-8"))
