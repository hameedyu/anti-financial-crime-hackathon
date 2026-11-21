"""Type-aware fact extraction from evidence documents.

Input is the generic :class:`ParsedMarkdown` produced at ingestion. Output is a
plain dict per document ("document facts") containing only what the text
states, each item tagged with where in the document it came from:

* ``parties``     names with the role the document gives them
* ``amounts``     money amounts with their label (field name, table column or
                  the words before them)
* ``accounts``    account identifiers (sort code + number, "ending 1234")
* ``references``  invoice / booking / order / claim numbers
* ``rows``        one entry per table row (name, amount, date, reference ...)
* ``ownership``   statements that an account is held by someone
* ``registry``    company number, officers and controllers with their dates
* ``signals``     narrative phrases worth an analyst's attention, from a fixed,
                  documented phrase list (heuristic, always quoted verbatim)
* ``claims``      statements made by the customer (never treated as verified)

Document types are mapped to a *reliability* class (who produced the
document) and a *category* (what it is for). Unknown types are classified from
their title; anything unrecognised stays ``unknown`` rather than guessed.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from data_processing.models import EvidenceDocument
from data_processing.textutil import (
    find_account_refs, find_money, find_text_dates, parse_month_year, parse_text_date,
)
from .models import Reliability

R = Reliability

TYPE_CLASS: dict[str, tuple[str, str]] = {
    # type: (reliability, category)
    "invoice": (R.THIRD_PARTY, "payment_support"),
    "dealer_invoice": (R.THIRD_PARTY, "payment_support"),
    "hospital_invoice": (R.THIRD_PARTY, "payment_support"),
    "supplier_invoice": (R.THIRD_PARTY, "payment_support"),
    "supplier_invoices": (R.THIRD_PARTY, "payment_support"),
    "carrier_invoice": (R.THIRD_PARTY, "payment_support"),
    "freight_invoices": (R.THIRD_PARTY, "payment_support"),
    "consulting_contract": (R.THIRD_PARTY, "payment_support"),
    "supply_contract": (R.THIRD_PARTY, "payment_support"),
    "contract_schedule": (R.THIRD_PARTY, "payment_support"),
    "purchase_order": (R.THIRD_PARTY, "payment_support"),
    "credit_note": (R.THIRD_PARTY, "payment_support"),
    "receipt": (R.THIRD_PARTY, "payment_support"),
    "booking_confirmation": (R.THIRD_PARTY, "payment_support"),
    "venue_email": (R.THIRD_PARTY, "payment_support"),
    "holding_deposit_receipt": (R.THIRD_PARTY, "payment_support"),
    "insurance_schedule": (R.THIRD_PARTY, "supporting_context"),
    "advert": (R.THIRD_PARTY, "supporting_context"),
    "insurer_letter": (R.THIRD_PARTY, "source_of_funds_event"),
    "solicitor_letter": (R.THIRD_PARTY, "source_of_funds_event"),
    "loan_agreement": (R.THIRD_PARTY, "source_of_funds_event"),
    "completion_statement": (R.THIRD_PARTY, "source_of_funds_event"),
    "cancellation_confirmation": (R.THIRD_PARTY, "source_of_funds_event"),
    "redundancy_letter": (R.THIRD_PARTY, "supporting_context"),
    "employment_contract": (R.THIRD_PARTY, "supporting_context"),
    "settlement_letter": (R.THIRD_PARTY, "source_of_funds_event"),
    "deposit_return_letter": (R.THIRD_PARTY, "source_of_funds_event"),
    "vehicle_sale_receipt": (R.THIRD_PARTY, "source_of_funds_event"),
    "sale_receipt": (R.THIRD_PARTY, "source_of_funds_event"),
    "factoring_agreement": (R.THIRD_PARTY, "business_records"),
    "remittance_advice": (R.THIRD_PARTY, "business_records"),
    "bank_statement": (R.THIRD_PARTY, "account_ownership"),
    "company_register_extract": (R.THIRD_PARTY, "corporate_registry"),
    "significant_control_register": (R.THIRD_PARTY, "corporate_registry"),
    "sending_bank_fraud_report": (R.THIRD_PARTY, "fraud_report"),
    "proof_of_address": (R.THIRD_PARTY, "kyc"),
    "gift_letter": (R.THIRD_PARTY_DECLARATION, "source_of_funds_event"),
    "due_diligence_summary": (R.INSTITUTION, "kyc"),
    "reverification_record": (R.INSTITUTION, "kyc"),
    "contact_note": (R.INSTITUTION, "correspondence"),
    "staff_note": (R.INSTITUTION, "correspondence"),
    "device_export": (R.INSTITUTION, "device"),
    "management_accounts": (R.CUSTOMER_RECORD, "business_records"),
    "cash_collection_reports": (R.CUSTOMER_RECORD, "business_records"),
    "cash_handling_procedure": (R.CUSTOMER_RECORD, "business_records"),
    "cash_reconciliation": (R.CUSTOMER_RECORD, "business_records"),
    "takings_book": (R.CUSTOMER_RECORD, "business_records"),
    "ticket_report": (R.CUSTOMER_RECORD, "business_records"),
    "payment_file": (R.CUSTOMER_RECORD, "business_records"),
    "group_structure": (R.CUSTOMER_RECORD, "corporate_registry"),
    "support_chat": (R.CUSTOMER_STATEMENT, "correspondence"),
    "customer_email": (R.CUSTOMER_STATEMENT, "correspondence"),
    "customer_letter": (R.CUSTOMER_STATEMENT, "correspondence"),
    "email": (R.CUSTOMER_STATEMENT, "correspondence"),
}

NON_TRANSACTIONAL = {"kyc", "corporate_registry", "device"}

# role of a party by the field label that names it
PARTY_ROLES = {
    "pay to": "payee", "supplier": "payee", "seller": "payee", "carrier": "payee", "payee": "payee",
    "bill to": "payer", "buyer": "payer", "patient": "payer", "lead traveller": "payer",
    "customer": "payer", "client": "client", "borrower": "beneficiary", "lender": "payer",
    "account holder": "account_holder", "policyholder": "subject", "company": "subject",
    "from": "issuer", "insurer": "issuer", "exporter": "payer", "contractor": "payee",
    "owner": "owner", "person": "controller", "officer": "officer",
}

_TITLE_PARTY = [
    (re.compile(r"^Register of people with significant control for (.+)$"), "subject"),
    (re.compile(r"^Company register extract for (.+)$"), "subject"),
    (re.compile(r"^Cash (?:handling procedure|reconciliation sheets?)(?:,| for) (.+)$"), "subject"),
    (re.compile(r"^Ticket sales report for (.+)$"), "subject"),
    (re.compile(r"\b(?:issued by|from)\s+(.+)$"), "issuer"),
    (re.compile(r"\bwith\s+(.+)$"), "counterparty"),
]
_TITLE_REF = re.compile(r"^(?:Invoice|Booking confirmation)\s+((?:[A-Z]+[ /-]?)?\d[\w/-]*|[A-Z]{2,}\s\d+)\s+from\b")

_REF_PATTERNS = [
    re.compile(r"\b(BOOKING|CLAIM|PO|INV|INVOICE|ORDER|FREIGHT|CARRIER)\.?\s+(?:NO\.\s+)?(\d{2,})\b", re.IGNORECASE),
    re.compile(r"\b([A-Z]{2,4})/(\d{3,})\b"),
    re.compile(r"\b(C)-(\d{3,})\b"),
]
_REF_FIELD_KEYS = ("reference", "invoice", "order", "contract", "claim", "booking", "credit for")

# Narrative signals: (signal, polarity, compiled pattern). Matching is literal;
# the matched sentence is always quoted so a reader can judge it.
_SIGNALS: list[tuple[str, str, re.Pattern]] = [(s, p, re.compile(rx, re.IGNORECASE)) for s, p, rx in [
    ("threshold_inquiry", "risk", r"(aggregat\w+|below the £[\d,]+|threshold|exempt\w* (?:them|it|me) from)"),
    ("intends_to_split_across_locations", "risk", r"visit a different branch|different (?:branch|location|outlet)s? for future"),
    ("acting_for_others", "risk", r"send it on|sending it on|send it to the company|keep \d+%|my manager needs|payments assistant|"
                                   r"clients pay into my account|friends sent me money|money to invest for them"),
    ("unwilling_to_explain", "risk", r"unwilling to explain|refused to explain|declined to explain|would not explain|could not explain"),
    ("crypto_counterparty", "context", r"\bcrypto\w*\b|\bbitcoin\b|\bcoin\w* exchange\b|\bExchange Ltd\b"),
    ("gambling_counterparty", "context", r"\bcasino\b|\bbetting\b|\bgambl\w+"),
    ("fraud_report", "risk", r"authorised push payment scam|request to freeze funds|scam"),
    ("dissolved_company", "risk", r"\bdissolved on\b"),
    ("third_party_paying_on_behalf", "context", r"will be sending funds to cover|paying on (?:my|our) behalf|on behalf of"),
    ("states_common_ownership", "explanatory", r"both companies are mine|companies are (?:both )?owned by|is held by|held in my name|in your name"),
    ("relocation_or_absence", "explanatory", r"relocated|working in \w+ for the past|trading paused|moved premises"),
    ("identity_reverified", "explanatory", r"identity re-verified"),
    ("job_found_online", "risk", r"advert on social media|it is all online|remote job"),
]]

_ADDRESS = re.compile(
    r"\b(?:Unit \w+,? |\d{1,4}[A-Z]? )[A-Z][a-z]+(?: [A-Z][a-z]+)*"
    r" (?:Works|Street|Road|Lane|Way|Avenue|Close|Place|Terrace|Gardens|Crescent|Drive|Court|Park|Square)"
    r"(?:, [A-Z][a-z]+(?: [A-Z][a-z]+)*)?(?:, [A-Z]{1,2}\d{1,2} \d[A-Z]{2})?")
_PERIOD = re.compile(r"(\d{1,2} [A-Z][a-z]+ \d{4}) to (\d{1,2} [A-Z][a-z]+ \d{4})")
_YEAR_TO = re.compile(r"\byear to (\d{1,2} [A-Z][a-z]+ \d{4})")
_STATEMENT_LINE = re.compile(r"^(\d{1,2} [A-Z][a-z]+ \d{4}):\s*(.+)$")


def classify(doc_type: str, title: str) -> tuple[str, str, str]:
    """Return ``(reliability, category, classification_source)``."""
    if doc_type in TYPE_CLASS:
        rel, cat = TYPE_CLASS[doc_type]
        return rel, cat, "type_map"
    t = (title or "").lower()
    if "invoice" in t or "contract" in t or "receipt" in t:
        return R.THIRD_PARTY, "payment_support", "title_keyword"
    if "from the customer" in t or t.startswith("chat"):
        return R.CUSTOMER_STATEMENT, "correspondence", "title_keyword"
    if "statement" in t:
        return R.THIRD_PARTY, "account_ownership", "title_keyword"
    return R.UNKNOWN, "unclassified", "unknown_type"


def _label_before(text: str, start: int) -> str:
    window = text[max(0, start - 60):start]
    window = re.split(r"[.:;!?\n]", window)[-1]
    words = re.findall(r"[A-Za-z']+", window)
    return " ".join(words[-4:]).lower()


def _refs_in(text: str) -> list[str]:
    out = []
    for rx in _REF_PATTERNS:
        for m in rx.finditer(text or ""):
            out.append(f"{m.group(1).upper()} {m.group(2)}")
    return out


def reference_keys(text: str | None) -> set[str]:
    """Comparison keys for references: digit runs of 3+ characters plus slash forms."""
    if not text:
        return set()
    keys = set(re.findall(r"\b[A-Z]{2,4}/\d{3,}\b", text.upper()))
    keys |= {d for d in re.findall(r"\d{3,}", text)}
    return keys


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return text[left:right].strip()


def extract_document_facts(doc: EvidenceDocument, customer_name: str | None = None) -> dict[str, Any]:
    reliability, category, cls_src = classify(doc.type, doc.title)
    facts: dict[str, Any] = {
        "doc_id": doc.doc_id, "type": doc.type, "title": doc.title, "dated": doc.dated,
        "relative_path": doc.relative_path, "media_type": doc.media_type, "read_status": doc.read_status,
        "reliability": reliability, "category": category, "classification_source": cls_src,
        "transactional": category not in NON_TRANSACTIONAL,
        "parties": [], "amounts": [], "accounts": [], "references": [], "dates": [],
        "period": None, "rows": [], "ownership": [], "registry": None, "signals": [], "claims": [],
        "fields": {}, "extraction": {"method": "markdown_fields+tables+narrative_patterns", "confidence": "none"},
    }
    p = doc.parsed
    if p is None or doc.text is None:
        facts["extraction"] = {"method": None, "confidence": "none", "note": f"document not readable ({doc.read_status})"}
        return facts
    text = doc.text
    body_lines = list(p.paragraphs)

    # -------------------------------------------------- title
    title = p.title or doc.title or ""
    for rx, role in _TITLE_PARTY:
        m = rx.search(title)
        if m:
            nm = re.sub(r",\s*[A-Z][\w ]+$", "", m.group(1)).strip()  # drop ", Italy"-style suffix
            facts["parties"].append({"role": role, "name": nm, "source": "title"})
            break
    if m := _TITLE_REF.search(title):
        facts["references"].append({"value": m.group(1), "source": "title"})

    # -------------------------------------------------- fields
    is_chat = doc.type == "support_chat"
    for key, val in p.fields:
        k = key.strip().lower()
        if is_chat and k in ("customer", "agent"):
            if k == "customer":
                facts["claims"].append({"claim_type": "customer_statement", "text": val, "made_by": "customer",
                                        "source": "chat turn", "status": "claimed_only"})
                for val_amt, ccy, s, e in find_money(val):  # amounts the customer states (claims, never proof)
                    facts["amounts"].append({"value": val_amt, "currency": ccy, "label": "customer statement",
                                             "source": "chat turn", "row": None, "sentence": val})
            continue
        facts["fields"].setdefault(key, val)
        role = PARTY_ROLES.get(k)
        if role and category != "kyc" and not (doc.type == "sending_bank_fraud_report" and k == "from"):
            nm, _, rest = val.partition(",")
            nm = nm.strip()
            if nm and not re.match(r"^(IBAN|account|HK account)\b", nm, re.IGNORECASE):
                facts["parties"].append({"role": role, "name": nm, "source": f"field:{key}",
                                         "location": rest.strip() or None})
        for val_amt, ccy, s, e in find_money(val):
            facts["amounts"].append({"value": val_amt, "currency": ccy, "label": k, "source": f"field:{key}",
                                     "row": None})
        for ref in find_account_refs(val):
            facts["accounts"].append({**ref, "source": f"field:{key}", "holder_hint": None})
        if any(rk in k for rk in _REF_FIELD_KEYS):
            facts["references"].append({"value": val, "source": f"field:{key}"})
        d = parse_text_date(val)
        if d:
            facts["dates"].append({"date": d, "label": k, "source": f"field:{key}"})
        if m := _PERIOD.search(val):
            facts["period"] = {"from": parse_text_date(m.group(1)), "to": parse_text_date(m.group(2)), "source": f"field:{key}"}

    # -------------------------------------------------- tables
    for ti, tbl in enumerate(p.tables):
        for ri, row in enumerate(tbl.rows):
            r: dict[str, Any] = {"table": ti, "row": ri, "cells": dict(row), "name": None, "amounts": [],
                                 "date": None, "reference": None, "country": None}
            first_header = tbl.headers[0] if tbl.headers else None
            for h in tbl.headers:
                cell = row.get(h, "")
                hl = h.lower()
                money = find_money(cell)
                if money:
                    for val_amt, ccy, s, e in money:
                        a = {"value": val_amt, "currency": ccy, "label": hl, "source": f"table{ti}.row{ri}:{h}", "row": ri}
                        r["amounts"].append(a)
                        facts["amounts"].append(a)
                    continue
                d = parse_text_date(cell)
                if d and r["date"] is None and hl not in ("born",):
                    r["date"] = d
                if hl in ("invoice", "contract", "booking", "order", "safe bag", "reference", "stage"):
                    r["reference"] = cell
                if hl == "country":
                    r["country"] = cell
            if first_header and first_header.lower() in ("officer", "person", "person with significant control",
                                                         "contractor", "payee", "beneficiary", "name", "supplier",
                                                         "employee", "company", "exporter"):
                r["name"] = row.get(first_header)
            facts["rows"].append(r)
            if r["reference"]:
                facts["references"].append({"value": r["reference"], "source": f"table{ti}.row{ri}"})
            nm_role = PARTY_ROLES.get((first_header or "").lower())
            if nm_role and r["name"]:
                facts["parties"].append({"role": nm_role, "name": r["name"], "source": f"table{ti}.row{ri}"})
            for h in tbl.headers:  # e.g. group structure "Owner", freight "Exporter"
                hr = PARTY_ROLES.get(h.lower())
                if hr and h != first_header and row.get(h):
                    facts["parties"].append({"role": hr, "name": row[h], "source": f"table{ti}.row{ri}:{h}"})

    # -------------------------------------------------- narrative
    for line in body_lines:
        for val_amt, ccy, s, e in find_money(line):
            facts["amounts"].append({"value": val_amt, "currency": ccy, "label": _label_before(line, s),
                                     "source": "narrative", "row": None, "sentence": _sentence_around(line, s, e)})
        for ref in find_account_refs(line):
            holder = None
            if m := re.search(re.escape(ref["raw"]) + r"\s+is held by\s+([^.]+)", line):
                holder = m.group(1).strip()
            elif re.search(re.escape(ref["raw"]) + r"[^.]*\b(?:in your name|held in my name|in my name)\b", line):
                holder = "__customer__"
            facts["accounts"].append({**ref, "source": "narrative", "holder_hint": holder})
        m = _STATEMENT_LINE.match(line)
        if m:
            money = find_money(m.group(2))
            facts["rows"].append({"table": None, "row": len(facts["rows"]), "cells": {"line": line},
                                  "name": None, "date": parse_text_date(m.group(1)),
                                  "amounts": [{"value": money[0][0], "currency": money[0][1], "label": "statement entry",
                                               "source": "narrative", "row": None}] if money else [],
                                  "reference": None, "country": None, "text": m.group(2)})
        if m := re.match(r"^\d+\.\s+(.+)$", line):  # numbered lists (e.g. travellers)
            facts["rows"].append({"table": None, "row": len(facts["rows"]), "cells": {"item": m.group(1)},
                                  "name": m.group(1).strip(), "date": None, "amounts": [], "reference": None,
                                  "country": None, "list_item": True})
        if m := re.match(r"^Dear ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+),", line):
            facts["parties"].append({"role": "addressee", "name": m.group(1), "source": "narrative:salutation"})
        if m := _PERIOD.search(line):
            facts["period"] = facts["period"] or {"from": parse_text_date(m.group(1)), "to": parse_text_date(m.group(2)),
                                                  "source": "narrative"}
    if m := _YEAR_TO.search(title):
        end = parse_text_date(m.group(1))
        if end:
            facts["period"] = {"from": end.replace(year=end.year - 1) + timedelta(days=1) if not (end.month == 2 and end.day == 29) else end - timedelta(days=365),
                               "to": end, "source": "title"}

    for ref in _refs_in(text):
        if ref.replace(" ", "-") != doc.doc_id:
            facts["references"].append({"value": ref, "source": "pattern"})
    # de-duplicate references by value
    seen = set()
    facts["references"] = [r for r in facts["references"] if not (r["value"] in seen or seen.add(r["value"]))]
    for d in find_text_dates("\n".join(body_lines)):
        facts["dates"].append({"date": d, "label": "narrative", "source": "narrative"})

    # -------------------------------------------------- type-specific structure
    _type_specific(doc, p, text, facts, customer_name)

    # -------------------------------------------------- signals
    for sig, polarity, rx in _SIGNALS:
        for m in rx.finditer(text):
            sentence = _sentence_around(text, m.start(), m.end())
            if any(s["signal"] == sig and s["quote"] == sentence for s in facts["signals"]):
                continue
            facts["signals"].append({"signal": sig, "polarity": polarity, "phrase": m.group(0), "quote": sentence,
                                     "heuristic": True})

    # several listed people sharing one address (e.g. payees in a payment file): context for an analyst
    shared: dict[str, list[str]] = {}
    for r in facts["rows"]:
        addr = (r.get("cells") or {}).get("Address")
        if addr and r.get("name"):
            shared.setdefault(addr, []).append(r["name"])
    facts["shared_addresses"] = {a: n for a, n in sorted(shared.items()) if len(n) > 1}
    facts["addresses_mentioned"] = sorted({m.group(0).rstrip(".") for m in _ADDRESS.finditer("\n".join(body_lines))})

    n_items = len(facts["amounts"]) + len(facts["parties"]) + len(facts["rows"])
    facts["extraction"]["confidence"] = "high" if p.fields or p.tables else ("medium" if n_items else "low")
    return facts


def _type_specific(doc: EvidenceDocument, p, text: str, facts: dict, customer_name: str | None) -> None:
    t = doc.type
    f = {k.lower(): v for k, v in p.fields}

    if t in ("company_register_extract", "significant_control_register"):
        reg: dict[str, Any] = {"company": f.get("company") or next((x["name"] for x in facts["parties"] if x["role"] == "subject"), None),
                               "company_number": f.get("company number"), "registered_office": f.get("registered office"),
                               "nature_of_business": f.get("nature of business"), "incorporated": parse_text_date(f.get("incorporated", "")),
                               "status": f.get("status"), "dissolved_on": None, "officers": [], "controllers": []}
        if reg["status"] and (m := re.search(r"Dissolved on (.+)", reg["status"])):
            reg["dissolved_on"] = parse_text_date(m.group(1))
        for tbl in p.tables:
            h0 = tbl.headers[0].lower() if tbl.headers else ""
            for row in tbl.rows:
                born = row.get("Born")
                entry = {"name": row.get(tbl.headers[0]), "born": born, "born_month_year": parse_month_year(born) if born else None}
                if h0 == "officer":
                    entry.update(role=row.get("Role"), appointed=parse_text_date(row.get("Appointed", "")),
                                 resigned=parse_text_date(row.get("Resigned", "")))
                    reg["officers"].append(entry)
                elif h0 in ("person", "person with significant control"):
                    entry.update(control=row.get("Control"), notified=parse_text_date(row.get("Notified", "")),
                                 ceased=parse_text_date(row.get("Ceased", "")))
                    reg["controllers"].append(entry)
        facts["registry"] = reg

    elif t == "group_structure":
        facts["registry"] = {"group_members": [{"company": r["cells"].get("Company"), "owner": r["cells"].get("Owner"),
                                                "share": r["cells"].get("Share")} for r in facts["rows"]]}

    elif t == "bank_statement":
        holder = f.get("account holder")
        acct = f.get("account")
        refs = find_account_refs(acct or "")
        facts["ownership"].append({"holder": holder, "bank": f.get("bank"), "account": acct,
                                   "account_refs": refs, "source": "statement header",
                                   "statement": "account holder named on a statement issued by the bank"})
        for a in facts["accounts"]:
            if a.get("holder_hint") and a["holder_hint"] != "__customer__":
                facts["ownership"].append({"holder": a["holder_hint"], "bank": f.get("bank"), "account": a["raw"],
                                           "account_refs": [a], "source": "statement narrative",
                                           "statement": "statement says the account is held by this party"})

    elif t in ("cancellation_confirmation", "customer_letter"):
        for a in facts["accounts"]:
            if a.get("holder_hint") == "__customer__":
                facts["ownership"].append({"holder": customer_name, "bank": None, "account": a["raw"], "account_refs": [a],
                                           "source": "narrative",
                                           "statement": "document says the account is in the customer's name"})

    elif t == "sending_bank_fraud_report":
        m = re.search(r"Our customer (.+?) sent (£[\d,]+(?:\.\d+)?) on (\d{1,2} [A-Z][a-z]+ \d{4}) to your customer's "
                      r"account ending (\d+), reference \"([^\"]*)\"", text)
        rep = {"reporting_bank": next((x["name"] for x in facts["parties"] if x["role"] == "issuer"), None)}
        if m:
            amt = find_money(m.group(2))[0][0]
            rep.update(victim=m.group(1), amount=amt, sent_on=parse_text_date(m.group(3)),
                       account_last_digits=m.group(4), reference=m.group(5))
            facts["parties"].append({"role": "payer", "name": m.group(1), "source": "fraud report"})
        if m2 := re.search(r"They tell us (.+?)\.", text):
            rep["victim_account"] = m2.group(1)
        facts["fraud_report"] = rep

    elif t == "gift_letter":
        m = re.search(r"I, ([^,]+), born ([^,]+), hereby confirm that I am providing a gift of (£[\d,]+(?:\.\d+)?) to my ([a-z ]+), ([^.]+?)\.", text)
        if m:
            facts["parties"].append({"role": "payer", "name": m.group(1), "source": "gift letter declarant"})
            facts["parties"].append({"role": "beneficiary", "name": m.group(5).strip(), "source": "gift letter recipient"})
            facts["gift"] = {"donor": m.group(1), "donor_born": parse_text_date(m.group(2)),
                             "amount": find_money(m.group(3))[0][0], "relationship": m.group(4).strip(),
                             "recipient": m.group(5).strip(), "repayable": "no requirement for repayment" not in text}

    elif t in ("vehicle_sale_receipt", "sale_receipt"):
        m = re.search(r"Received from ([^,]+?) the sum of", text) or re.search(r"from ([A-Z][\w-]+ [A-Z][\w-]+) to ([A-Z][\w-]+ [A-Z][\w-]+)", text)
        if m and "Received from" in m.group(0):
            facts["parties"].append({"role": "payer", "name": m.group(1).strip(), "source": "receipt narrative"})
        elif m:
            facts["parties"].append({"role": "payee", "name": m.group(1), "source": "receipt narrative (seller)"})
            facts["parties"].append({"role": "payer", "name": m.group(2), "source": "receipt narrative (buyer)"})
        facts["paid_in_cash"] = bool(re.search(r"\bin cash\b|via cash|paid in full via cash", text, re.IGNORECASE))

    elif t == "device_export":
        devices = []
        for r in facts["rows"]:
            c = r["cells"]
            custs = [x.strip() for x in (c.get("Customers who logged in from it") or "").split(",") if x.strip()]
            countries = [x.strip() for x in (c.get("Login countries") or "").split(",") if x.strip()]
            devices.append({"device": c.get("Device"), "first_seen": parse_text_date(c.get("First seen", "")),
                            "customers": custs, "login_countries": countries})
        facts["devices"] = devices

    elif t in ("insurer_letter", "solicitor_letter", "settlement_letter", "deposit_return_letter",
               "completion_statement", "loan_agreement", "cancellation_confirmation"):
        # who is to receive the money, when the letter says so
        m = re.search(r"payment of (£[\d,]+(?:\.\d+)?) to ([A-Z][^.]+?) via", text)
        if m:
            facts["parties"].append({"role": "beneficiary", "name": m.group(2).strip(), "source": "narrative"})
