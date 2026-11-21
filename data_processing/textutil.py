"""Small, deterministic text helpers shared by ingestion and fact extraction.

Nothing here decides identity: name normalisation only produces comparison
keys that are used to *propose* candidate links.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
    re.IGNORECASE)
_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
    re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Money: "£1,234.56", "£1234", "GBP 1,234.56", "$1,234", "€12"
_MONEY = re.compile(
    r"(?P<sym>£|\$|€|GBP\s?|USD\s?|EUR\s?)(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_SYMBOL_CCY = {"£": "GBP", "$": "USD", "€": "EUR", "GBP": "GBP", "USD": "USD", "EUR": "EUR"}

# Account identifiers seen in the data: "12-34-56 12345678", "account ending 1234",
# "IBAN ending 1234", "HK account ending 1234"
_SORT_ACC = re.compile(r"\b(\d{2}-\d{2}-\d{2})\s+(\d{8})\b")
_ENDING = re.compile(r"\b((?:[A-Z]{2}\s)?(?:account|IBAN)\s+ending)\s+(\d{3,6})\b", re.IGNORECASE)

_COMPANY_SUFFIXES = (
    "limited", "ltd", "plc", "llp", "llc", "inc", "co", "company", "sarl", "sal", "srl", "fze", "bv",
    "gmbh", "sa", "sp z o o", "sp. z o.o.", "spa",
)
_SUFFIX_RE = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in sorted(_COMPANY_SUFFIXES, key=len, reverse=True)) + r")\.?$")


def parse_text_date(text: str) -> date | None:
    """First full date in ``text`` ("3 November 2019" or "2019-11-03")."""
    m = _DAY_MONTH_YEAR.search(text or "")
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    m = _ISO_DATE.search(text or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def find_text_dates(text: str) -> list[date]:
    out: list[date] = []
    for m in _DAY_MONTH_YEAR.finditer(text or ""):
        try:
            out.append(date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))))
        except ValueError:
            continue
    for m in _ISO_DATE.finditer(text or ""):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return out


def parse_month_year(text: str) -> tuple[int, int] | None:
    """``"August 1978"`` -> ``(1978, 8)``. A full date also yields its month and year."""
    d = parse_text_date(text or "")
    if d:
        return d.year, d.month
    m = _MONTH_YEAR.search(text or "")
    if m:
        return int(m.group(2)), MONTHS[m.group(1).lower()]
    return None


def find_money(text: str) -> list[tuple[Decimal, str, int, int]]:
    """All money amounts: ``(amount, currency, start, end)``."""
    out = []
    for m in _MONEY.finditer(text or ""):
        sym = m.group("sym").strip()
        try:
            val = Decimal(m.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        out.append((val, _SYMBOL_CCY.get(sym, sym), m.start(), m.end()))
    return out


def parse_money(text: str) -> tuple[Decimal, str] | None:
    found = find_money(text)
    return (found[0][0], found[0][1]) if found else None


def find_account_refs(text: str) -> list[dict]:
    """Account identifiers with a normalised form for comparison."""
    refs = []
    for m in _SORT_ACC.finditer(text or ""):
        refs.append({"raw": m.group(0), "kind": "sort_code_account", "sort_code": m.group(1),
                     "number": m.group(2), "last_digits": m.group(2)[-4:]})
    for m in _ENDING.finditer(text or ""):
        refs.append({"raw": m.group(0), "kind": "ending", "label": m.group(1),
                     "number": None, "last_digits": m.group(2)[-4:]})
    return refs


def normalize_name(name: str | None) -> str:
    """Lower-case, punctuation-free, single-spaced comparison key."""
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def core_name(name: str | None) -> str:
    """``normalize_name`` with trailing legal-form suffixes and "client account" removed."""
    s = normalize_name(name)
    s = re.sub(r"\bclient account\b", "", s).strip()
    prev = None
    while prev != s:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
    return s


def name_tokens(name: str | None) -> set[str]:
    return {t for t in core_name(name).split() if len(t) > 1 and t != "and"}


def names_equal(a: str | None, b: str | None) -> bool:
    ca, cb = core_name(a), core_name(b)
    return bool(ca) and ca == cb


def token_overlap(a: str | None, b: str | None) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def normalize_address(addr: str | None) -> dict:
    """Split an address into a building/number part and the remainder, for conservative comparison."""
    if not addr:
        return {"raw": None, "key": "", "premises": "", "rest": ""}
    key = normalize_name(addr)
    parts = [normalize_name(p) for p in addr.split(",") if p.strip()]
    if parts and re.fullmatch(r"(unit|flat) \w+|\d+\w?", parts[0]) and len(parts) > 1:
        premises, street, rest = parts[0], parts[1], " ".join(parts[2:])  # "Unit 4B, Corden Works, Dunwick"
    else:
        first = parts[0] if parts else ""
        m = re.match(r"^(unit \w+|flat \w+|\d+\w?)\s+(.*)$", first)
        premises, street = (m.group(1), m.group(2)) if m else ("", first)
        rest = " ".join(parts[1:])
    return {"raw": addr, "key": key, "premises": premises, "street": street, "rest": rest}


def round_amount(value: Decimal) -> bool:
    """Context flag only: whole hundreds of pounds."""
    return value == value.to_integral_value() and int(value) % 100 == 0 and value > 0
