"""Candidate links between documents and transactions.

A link is proposed only when an amount in the document equals a transaction
amount (to the penny, in GBP or in the transaction's own currency), equals the
sum of a group of transactions, or shares a reference number with an alerted
transaction. Each proposal is then graded field by field:

=================  ==========================================================
field              outcome
=================  ==========================================================
amount             match (exact) / partial (within 0.5 %, e.g. FX rounding)
counterparty       match (same name, legal suffixes ignored) / partial
                   (shared name words) / conflict (the document names other
                   parties and none is the transaction's counterparty)
account            match (sort code + number) / partial (same last digits) /
                   conflict (both present, different)
reference          match (shared invoice/booking/order number) / conflict
                   (an invoice-type document and the payment cites a
                   different number)
date               match (payment 7 days before to 60 days after the document
                   or a date it names) / partial (further away)
=================  ==========================================================

Strength:
* ``strong``      amount match plus an account or reference match, or amount
                  + counterparty + date all matching; no conflicts
* ``moderate``    amount match plus counterparty name only; or an approximate
                  amount plus account/reference
* ``weak``        amount only, or reference only
* ``conflicting`` any conflict on counterparty, account or reference

A name that merely looks similar never makes a link stronger than
``moderate``, and a conflicting link is reported as ``contradicted``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from data_processing.models import Transaction
from data_processing.textutil import core_name, names_equal, token_overlap
from .documents import reference_keys
from .metrics import ZERO, counterparty_key, group_by, sort_tx, total
from .models import DocumentMatch, EvidenceStatus, FieldComparison, Reliability

APPROX = Decimal("0.005")
MIN_AMOUNT = Decimal("20")
INVOICE_LIKE = {"payment_support"}
PROFILE_LABELS = ("declared monthly income", "expected annual payment volume", "turnover", "cash sales", "card sales",
                  "cost of sales", "net profit")


def _amount_outcome(doc_amt: Decimal, ccy: str, t: Transaction) -> tuple[str, str]:
    # Approximate equality is only meaningful where a currency conversion is involved.
    fx = t.currency != "GBP" or ccy != "GBP"
    if ccy == "GBP":
        if doc_amt == t.amount_gbp:
            return "match", "GBP amount equal"
        if fx and t.amount_gbp and abs(doc_amt - t.amount_gbp) / t.amount_gbp <= APPROX:
            return "partial", f"within 0.5% of the GBP amount ({t.amount_gbp}) of a {t.currency} payment"
    if ccy == t.currency:
        if doc_amt == t.amount:
            return "match", f"{ccy} amount equal"
        if fx and t.amount and abs(doc_amt - t.amount) / t.amount <= APPROX:
            return "partial", f"within 0.5% of the {ccy} amount"
    return "conflict", "amount differs"


def _other_parties(doc: dict, customer_name: str | None) -> list[dict]:
    out = []
    for p in doc["parties"]:
        if customer_name and names_equal(p["name"], customer_name):
            continue
        if p["role"] in ("addressee",):
            continue
        out.append(p)
    return out


def _row_people(doc: dict, customer_name: str | None) -> list[dict]:
    """Names listed in table rows or numbered lists (travellers, contractors, ...)."""
    out = []
    for r in doc["rows"]:
        nm = r.get("name")
        if nm and not (customer_name and names_equal(nm, customer_name)):
            out.append({"role": "listed_person", "name": nm, "source": f"row {r['row']}"})
    return out


def _compare_counterparty(doc: dict, t: Transaction, customer_name: str | None, row_name: str | None = None) -> FieldComparison:
    cp = t.counterparty_name
    parties = _other_parties(doc, customer_name)
    names = [p["name"] for p in parties]
    if not cp:
        return FieldComparison("counterparty", names or None, None, "absent", "transaction has no counterparty name (e.g. cash)")
    if row_name:
        # the amount came from a table row that names its own party: compare with that row only
        if names_equal(row_name, cp):
            return FieldComparison("counterparty", row_name, cp, "match", "party named on the same table row as the amount")
        if token_overlap(row_name, cp) >= 0.5:
            return FieldComparison("counterparty", row_name, cp, "partial", "shared name words with the row's party")
        return FieldComparison("counterparty", row_name, cp, "conflict", "the table row with this amount names a different party")
    for p in parties + _row_people(doc, customer_name):
        if names_equal(p["name"], cp):
            return FieldComparison("counterparty", p["name"], cp, "match", f"{p['role']} named in {p['source']}")
    if customer_name and names_equal(cp, customer_name):
        holders = [p for p in doc["parties"] if p["role"] == "account_holder" and names_equal(p["name"], customer_name)]
        if holders:
            return FieldComparison("counterparty", holders[0]["name"], cp, "match",
                                   "own-name transfer; the document names the customer as account holder")
    if not parties:
        return FieldComparison("counterparty", None, cp, "absent", "document names no other party")
    best = max(parties, key=lambda p: (token_overlap(p["name"], cp), p["name"]))
    ov = token_overlap(best["name"], cp)
    if ov >= 0.5:
        return FieldComparison("counterparty", best["name"], cp, "partial", f"shared name words (overlap {ov:.2f}); not proof of identity")
    if doc["category"] in INVOICE_LIKE | {"source_of_funds_event"}:
        return FieldComparison("counterparty", names, cp, "conflict", "document names other parties")
    return FieldComparison("counterparty", names, cp, "absent", "document type does not name the paying/receiving party")


def _compare_account(doc: dict, t: Transaction) -> FieldComparison:
    if doc["type"] == "sending_bank_fraud_report":
        return FieldComparison("account", None, t.counterparty_account, "absent", "fraud report cites the customer's own account")
    accs = [a for a in doc["accounts"]]
    ta = t.counterparty_account
    if t.direction == "in" and doc["category"] not in ("account_ownership",) and accs:
        # invoice/booking accounts are where the *payee* is paid; an inbound payment's counterparty
        # account is the payer's, so the two are not comparable
        return FieldComparison("account", [a["raw"] for a in accs], ta, "absent",
                               "document account is a payee account; not comparable with an inbound payment")
    if not accs or not ta:
        return FieldComparison("account", [a["raw"] for a in accs] or None, ta, "absent")
    from data_processing.textutil import find_account_refs
    trefs = find_account_refs(ta)
    for a in accs:
        for r in trefs:
            if a.get("number") and r.get("number"):
                if a["sort_code"] == r["sort_code"] and a["number"] == r["number"]:
                    return FieldComparison("account", a["raw"], ta, "match", "sort code and account number equal")
            elif a["last_digits"] == r["last_digits"]:
                # one side gives only the last digits: those digits are all that can be compared
                return FieldComparison("account", a["raw"], ta, "match", "last digits equal (only the last digits are given)")
    for a in accs:
        for r in trefs:
            if a["last_digits"] == r["last_digits"]:
                return FieldComparison("account", a["raw"], ta, "partial", "same last digits only")
    return FieldComparison("account", [a["raw"] for a in accs], ta, "conflict", "account details differ")


def _doc_ref_keys(doc: dict) -> set[str]:
    keys: set[str] = set()
    for r in doc["references"]:
        keys |= reference_keys(r["value"])
    return keys


def _compare_reference(doc: dict, t: Transaction) -> FieldComparison:
    dk = _doc_ref_keys(doc)
    tk = reference_keys(t.reference)
    if not dk or not tk:
        return FieldComparison("reference", sorted(dk) or None, t.reference, "absent")
    common = dk & tk
    if common:
        return FieldComparison("reference", sorted(common), t.reference, "match", "shared reference number")
    if doc["category"] in INVOICE_LIKE:
        return FieldComparison("reference", sorted(dk), t.reference, "conflict", "payment cites a different reference number")
    return FieldComparison("reference", sorted(dk), t.reference, "absent", "references differ; document type does not require one")


def _compare_date(doc: dict, t: Transaction, row_date=None) -> FieldComparison:
    cands = [d for d in [row_date, doc["dated"]] if d] + [d["date"] for d in doc["dates"] if d["label"] != "born"]
    if not cands:
        return FieldComparison("date", None, t.booked_date, "absent")
    best = min(cands, key=lambda d: abs((t.booked_date - d).days))
    diff = (t.booked_date - best).days
    outcome = "match" if -7 <= diff <= 60 else "partial"
    return FieldComparison("date", best, t.booked_date, outcome, f"payment {diff:+d} days from the document date")


def _grade(comps: list[FieldComparison], reliability: str, amount_outcome: str, *, no_counterparty_possible: bool = False,
           record_like: bool = False) -> tuple[str, str]:
    by = {c.field: c.outcome for c in comps}
    conflicts = [f for f in ("counterparty", "account", "reference") if by.get(f) == "conflict"]
    if conflicts:
        return "conflicting", EvidenceStatus.CONTRADICTED
    acct_ref = by.get("account") == "match" or by.get("reference") == "match"
    if amount_outcome == "match" and (acct_ref or (by.get("counterparty") == "match" and by.get("date") == "match")):
        strength = "strong"
    elif amount_outcome == "match" and by.get("date") == "match" and no_counterparty_possible and record_like:
        # cash has no counterparty: exact amount + date against a record of that cash is the most a document can show
        strength = "strong"
    elif (amount_outcome == "match" and by.get("counterparty") in ("match", "partial")) or \
            (amount_outcome == "partial" and acct_ref) or \
            (amount_outcome == "absent" and by.get("counterparty") == "match" and acct_ref):
        strength = "moderate"
    else:
        strength = "weak"
    if reliability == Reliability.CUSTOMER_STATEMENT:
        return strength, EvidenceStatus.CLAIMED_ONLY
    if strength == "strong" and reliability in (Reliability.THIRD_PARTY, Reliability.INSTITUTION):
        return strength, EvidenceStatus.VERIFIED
    return strength, EvidenceStatus.PARTIALLY_SUPPORTED


def _explain(doc: dict, tx_desc: str, comps: list[FieldComparison], strength: str) -> str:
    parts = [f"{c.field} {c.outcome}" + (f" ({c.note})" if c.note else "") for c in comps if c.outcome != "absent"]
    missing = [c.field for c in comps if c.outcome == "absent"]
    s = f"{doc['doc_id']} ({doc['type']}) vs {tx_desc}: " + "; ".join(parts)
    if missing:
        s += f". Not comparable: {', '.join(missing)}"
    return s + f". Strength {strength}."


def _make(doc: dict, txs: list[Transaction], kind: str, comps: list[FieldComparison], amt, label, amount_outcome) -> DocumentMatch:
    cashlike = all(t.is_cash or not t.counterparty_name for t in txs)
    record_like = doc["category"] == "business_records" or bool(doc.get("paid_in_cash"))
    strength, status = _grade(comps, doc["reliability"], amount_outcome, no_counterparty_possible=cashlike, record_like=record_like)
    if kind == "aggregate" and strength == "strong" and not any(c.field in ("account", "reference") and c.outcome == "match" for c in comps):
        strength = "moderate"
        status = EvidenceStatus.PARTIALLY_SUPPORTED if status == EvidenceStatus.VERIFIED else status
    desc = (txs[0].transaction_id if len(txs) == 1 else f"{len(txs)} transactions totalling {total(txs)}")
    return DocumentMatch(
        match_id="", doc_id=doc["doc_id"], doc_type=doc["type"], reliability=doc["reliability"],
        transaction_ids=[t.transaction_id for t in txs], match_kind=kind,
        matched_fields=[c.field for c in comps if c.outcome == "match"],
        partial_fields=[c.field for c in comps if c.outcome == "partial"],
        missing_fields=[c.field for c in comps if c.outcome == "absent"],
        conflicting_fields=[c.field for c in comps if c.outcome == "conflict"],
        comparisons=comps, strength=strength, verification_status=status,
        explanation=_explain(doc, desc, comps, strength), document_amount=amt, document_amount_label=label,
    )


def match_documents(doc_facts: list[dict], txs: list[Transaction], *, customer_name: str | None,
                    focus_ids: Iterable[str], groups: dict[str, list[Transaction]]) -> list[DocumentMatch]:
    """Propose and grade document-transaction links for one case.

    ``focus_ids`` are the transactions that matter most (alerted, held, window);
    reference-only links are proposed only for them. ``groups`` maps a label to a
    set of transactions whose *sum* a document total may explain (e.g. one
    alert's transactions, or one counterparty's payments in the window).
    """
    focus = set(focus_ids)
    txs = sort_tx(txs)
    out: list[DocumentMatch] = []
    for doc in doc_facts:
        if not doc["transactional"] or doc["read_status"] not in ("ok", "ok_non_utf8"):
            continue
        amounts = [a for a in doc["amounts"] if a["value"] >= MIN_AMOUNT and not any(l in a["label"] for l in PROFILE_LABELS)]
        row_dates = {a["source"]: next((r["date"] for r in doc["rows"] if r["row"] == a["row"] and r.get("table") is not None), None)
                     for a in amounts if a["row"] is not None}
        best: dict[str, tuple] = {}
        for a in amounts:
            for t in txs:
                outcome, note = _amount_outcome(a["value"], a["currency"], t)
                if outcome == "conflict":
                    continue
                rank = 0 if outcome == "match" else 1
                prev = best.get(t.transaction_id)
                if prev is None or rank < prev[0]:
                    best[t.transaction_id] = (rank, a, outcome, note)
        for t in txs:
            if t.transaction_id in best:
                continue
            if t.transaction_id in focus and _doc_ref_keys(doc) & reference_keys(t.reference):
                best[t.transaction_id] = (2, None, "absent", "no amount in the document equals this transaction")
        for t in txs:
            if t.transaction_id not in best:
                continue
            rank, a, outcome, note = best[t.transaction_id]
            if t.is_card and t.transaction_id not in focus and outcome != "match":
                continue
            row_name = None
            if a and a["row"] is not None and a["source"].startswith("table"):
                ti = int(a["source"][5:a["source"].index(".")])
                row_name = next((r["name"] for r in doc["rows"] if r.get("table") == ti and r["row"] == a["row"]), None)
            comps = [FieldComparison("amount", a["value"] if a else None, t.amount_gbp, outcome, note),
                     _compare_counterparty(doc, t, customer_name, row_name), _compare_account(doc, t),
                     _compare_reference(doc, t), _compare_date(doc, t, row_dates.get(a["source"]) if a else None)]
            if a and a["currency"] != "GBP":
                comps.append(FieldComparison("currency", a["currency"], t.currency,
                                             "match" if a["currency"] == t.currency else "conflict"))
            # an amount-only coincidence on an unrelated, unfocused transaction is noise
            if t.transaction_id not in focus and outcome == "match" and not any(
                    c.outcome == "match" for c in comps[1:4]):
                continue
            out.append(_make(doc, [t], "single", comps, a["value"] if a else None, a["label"] if a else None, outcome))

        # aggregates: one document amount = the sum of a group of transactions
        for label, group in groups.items():
            if len(group) < 2:
                continue
            gsum = total(group)
            for a in amounts:
                if a["currency"] != "GBP" or a["value"] < Decimal("100"):
                    continue
                if a["value"] == gsum:
                    outcome = "match"
                elif gsum and any(t.currency != "GBP" for t in group) and abs(a["value"] - gsum) / gsum <= APPROX:
                    outcome = "partial"
                else:
                    continue
                cps = {counterparty_key(t) for t in group}
                rep = group[0]
                comps = [FieldComparison("amount", a["value"], gsum, outcome, f"document amount vs sum of {label}")]
                cc = _compare_counterparty(doc, rep, customer_name) if len(cps) == 1 else \
                    FieldComparison("counterparty", None, sorted(cps), "absent", "group has several counterparties")
                comps += [cc, _compare_account(doc, rep), _compare_reference(doc, rep), _compare_date(doc, rep)]
                out.append(_make(doc, list(group), "aggregate", comps, a["value"], a["label"], outcome))
                break
    # A conflicting pairing is dropped when the same document matches another
    # transaction strongly (e.g. two own-account statements, each tied to one
    # transfer): the conflict only says "this is the other transfer".
    strong_docs = {m.doc_id for m in out if m.strength == "strong"}
    strong_tx = {t for m in out if m.strength == "strong" for t in m.transaction_ids}
    out = [m for m in out if not (m.strength == "conflicting" and m.doc_id in strong_docs
                                  and all(t in strong_tx for t in m.transaction_ids))]
    out.sort(key=lambda m: (m.doc_id, m.match_kind, m.transaction_ids))
    for i, m in enumerate(out, start=1):
        m.match_id = f"DM{i:03d}"
    return out


def table_coverage(doc: dict, matches: list[DocumentMatch]) -> dict | None:
    """For a document with table rows that carry amounts: how many rows a transaction matches."""
    rows = [r for r in doc["rows"] if r.get("amounts") and r.get("table") is not None]
    if not rows:
        return None
    single = [m for m in matches if m.doc_id == doc["doc_id"] and m.match_kind == "single"
              and m.strength != "conflicting"]
    amounts = {(m.document_amount) for m in single}
    covered = [r for r in rows if any(a["value"] in amounts for a in r["amounts"])]
    return {"doc_id": doc["doc_id"], "rows_with_amounts": len(rows), "rows_matched": len(covered),
            "matched_transaction_ids": sorted({t for m in single for t in m.transaction_ids})}
