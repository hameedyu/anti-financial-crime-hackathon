"""Internal-list match candidates (TM22 and proactive name screening).

The comparison is field by field and never upgrades a fuzzy name into an
identity. ``candidate_confidence`` values:

* ``identifier_conflict``     a reliable identifier on file (date of birth, or
                              month and year of birth from a register) differs
                              from the list entry: supports "a different party"
* ``identity_corroborated``   name plus an equal full date of birth
* ``probable``                name plus equal month and year of birth, or name
                              plus the same full address
* ``address_premises_differ`` same street/building but a different unit or
                              house number: supports "a different party" for an
                              address-only match
* ``possible``                one detail only (name only, or address only)
* ``none``                    nothing on file beyond the alert text
"""
from __future__ import annotations

from datetime import date

from data_processing.models import InternalListEntry, Transaction
from data_processing.textutil import core_name, names_equal, normalize_address
from .models import ListMatchCandidate


def people_on_file(doc_facts: list[dict]) -> list[dict]:
    """People named in documents together with identifying details."""
    out = []
    for d in doc_facts:
        reg = d.get("registry")
        if reg and "officers" in reg:
            for kind in ("officers", "controllers"):
                for p in reg.get(kind, []):
                    out.append({**p, "kind": kind[:-1], "born_full": None, "company": reg.get("company"),
                                "company_number": reg.get("company_number"),
                                "registered_office": reg.get("registered_office"), "doc_id": d["doc_id"]})
        g = d.get("gift")
        if g:
            out.append({"name": g["donor"], "kind": "gift_donor", "born": None, "born_full": g.get("donor_born"),
                        "born_month_year": None, "company": None, "doc_id": d["doc_id"]})
    return out


def compare_address(listed: str | None, other: str | None) -> dict:
    a, b = normalize_address(listed), normalize_address(other)
    if not a["raw"] or not b["raw"]:
        return {"outcome": "absent"}
    if a["key"] == b["key"]:
        return {"outcome": "match"}
    if a.get("street") and a["street"] == b.get("street"):
        if a["premises"] and b["premises"] and a["premises"] != b["premises"]:
            return {"outcome": "premises_differ", "listed_premises": a["premises"], "other_premises": b["premises"]}
        return {"outcome": "partial"}
    return {"outcome": "different"}


def _role_on(p: dict, on: date) -> str:
    start = p.get("appointed") or p.get("notified")
    end = p.get("resigned") or p.get("ceased")
    if start and on < start:
        return "not_yet_in_role"
    if end and on > end:
        return "role_ended_before_payment"
    if end and on == end:
        return "role_ended_on_payment_date"
    return "in_role_on_payment_date"


_CONTROL_RANK = {"in_role_on_payment_date": 0, "role_ended_on_payment_date": 1, "role_ended_before_payment": 2,
                 "not_yet_in_role": 3}


def assess_entry(entry: InternalListEntry, *, alert_ids: list[str], transactions: list[Transaction],
                 alert_matched_on: str | None, doc_facts: list[dict], customer_name: str | None) -> ListMatchCandidate:
    comps: list[dict] = []
    matched: list[str] = []
    conflicting: list[str] = []
    unknown: list[str] = []
    supporting: list[str] = []
    tx = transactions[0] if transactions else None
    cp = tx.counterparty_name if tx else None
    on = tx.booked_date if tx else None

    if cp:
        same = names_equal(entry.name, cp)
        comps.append({"field": "name", "listed": entry.name, "observed": cp, "source": tx.transaction_id,
                      "outcome": "match" if same else "not_counterparty_name",
                      "note": None if same else "listed name is not the counterparty's own name (may be an officer or controller)"})
        if same:
            matched.append("name")

    people = [p for p in people_on_file(doc_facts) if names_equal(p["name"], entry.name)]
    control: dict = {"status": "unknown", "detail": None, "doc_ids": []}
    for p in people:
        supporting.append(p["doc_id"])
        comps.append({"field": "name", "listed": entry.name, "observed": p["name"], "source": p["doc_id"],
                      "outcome": "match", "note": f"{p['kind'].replace('_', ' ')}" + (f" of {p['company']}" if p.get("company") else "")})
        matched.append("name")
        if entry.date_of_birth and p.get("born_full"):
            same = p["born_full"] == entry.date_of_birth
            comps.append({"field": "date_of_birth", "listed": entry.date_of_birth, "observed": p["born_full"],
                          "source": p["doc_id"], "outcome": "match" if same else "conflict"})
            (matched if same else conflicting).append("date_of_birth")
        elif entry.date_of_birth and p.get("born_month_year"):
            y, m = p["born_month_year"]
            same = (y, m) == (entry.date_of_birth.year, entry.date_of_birth.month)
            comps.append({"field": "month_year_of_birth", "listed": entry.date_of_birth.strftime("%B %Y"),
                          "observed": p["born"], "source": p["doc_id"], "outcome": "match" if same else "conflict"})
            (matched if same else conflicting).append("month_year_of_birth")
        if on and p["kind"] in ("officer", "controller"):
            r = _role_on(p, on)
            detail = (f"{p['name']} {p['kind']} of {p.get('company') or 'the company'} "
                      f"({p.get('role') or p.get('control') or ''}); {r.replace('_', ' ')} {on.isoformat()} per {p['doc_id']}")
            control["doc_ids"] = sorted(set(control["doc_ids"]) | {p["doc_id"]})
            control.setdefault("roles", []).append({"doc_id": p["doc_id"], "kind": p["kind"], "status": r,
                                                    "start": p.get("appointed") or p.get("notified"),
                                                    "end": p.get("resigned") or p.get("ceased")})
            if control["status"] == "unknown" or _CONTROL_RANK[r] < _CONTROL_RANK[control["status"]]:
                control["status"], control["detail"] = r, detail

    if cp and not people:
        regs = [p for p in people_on_file(doc_facts) if p.get("company") and names_equal(p["company"], cp)]
        if regs:
            comps.append({"field": "officers_on_register", "listed": entry.name,
                          "observed": sorted({p["name"] for p in regs}), "source": sorted({p["doc_id"] for p in regs}),
                          "outcome": "listed_name_absent"})
            supporting += [p["doc_id"] for p in regs]

    if entry.address:
        candidates = [(p["registered_office"], p["doc_id"]) for p in people_on_file(doc_facts) if p.get("registered_office")]
        for d in doc_facts:
            candidates += [(a, d["doc_id"]) for a in d.get("addresses_mentioned", [])]
        best = None
        for addr, did in candidates:
            c = compare_address(entry.address, addr)
            if c["outcome"] == "match":
                best = (c, addr, did)
                break
            if c["outcome"] in ("premises_differ", "partial") and best is None:
                best = (c, addr, did)
        if best:
            c, addr, did = best
            comps.append({"field": "address", "listed": entry.address, "observed": addr, "source": did, **c})
            supporting.append(did)
            {"match": matched, "premises_differ": conflicting, "partial": unknown}[c["outcome"]].append("address")
        elif alert_matched_on and "address" in alert_matched_on:
            unknown.append("address")

    for k, v in sorted(entry.identifiers.items()):
        comps.append({"field": f"identifier:{k}", "listed": v, "observed": None, "outcome": "absent",
                      "note": "no record on file carries this identifier type"})
    if entry.date_of_birth and not any(f in matched + conflicting for f in ("date_of_birth", "month_year_of_birth")):
        unknown.append("date_of_birth")

    for d in doc_facts:
        reg = d.get("registry") or {}
        if reg.get("dissolved_on"):
            comps.append({"field": "company_status", "observed": f"{reg.get('company')} dissolved on {reg['dissolved_on']}",
                          "source": d["doc_id"], "outcome": "context"})
            supporting.append(d["doc_id"])
    if customer_name and names_equal(entry.name, customer_name):
        comps.append({"field": "customer_name", "listed": entry.name, "observed": customer_name, "outcome": "match",
                      "note": "the listed name equals the customer's own name"})

    if conflicting and any(f in conflicting for f in ("date_of_birth", "month_year_of_birth")):
        conf = "identifier_conflict"
    elif "address" in conflicting and "name" not in matched:
        conf = "address_premises_differ"
    elif "name" in matched and "date_of_birth" in matched:
        conf = "identity_corroborated"
    elif "name" in matched and ("month_year_of_birth" in matched or "address" in matched):
        conf = "probable"
    elif matched:
        conf = "possible"
    else:
        conf = "none"

    return ListMatchCandidate(
        entry_id=entry.entry_id, alert_ids=alert_ids, transaction_ids=[t.transaction_id for t in transactions],
        counterparty=cp, listed_name=entry.name, listed_kind=entry.kind, listed_reason=entry.reason,
        alert_matched_on=alert_matched_on, matched_fields=sorted(set(matched)),
        conflicting_fields=sorted(set(conflicting)), unknown_fields=sorted(set(unknown)), comparisons=comps,
        control_relationship=control, candidate_confidence=conf, supporting_ids=sorted(set(supporting)),
        explanation=_explain(entry, conf, comps, control),
    )


def _explain(entry: InternalListEntry, conf: str, comps: list[dict], control: dict) -> str:
    bits = []
    for c in comps:
        if c.get("outcome") in ("match", "conflict", "premises_differ", "listed_name_absent"):
            bits.append(f"{c['field']} {c['outcome'].replace('_', ' ')} (list: {c.get('listed')}; on file: {c.get('observed')}; {c.get('source')})")
    s = f"{entry.entry_id} {entry.name}: " + ("; ".join(bits) if bits else "nothing on file corroborates or contradicts the match")
    if control["status"] != "unknown":
        s += f". {control['detail']}"
    return s + f". Candidate confidence: {conf}."


def screen_names(entries_by_core: dict, names: list[tuple[str, str]]) -> list[dict]:
    """Proactive screening: exact normalised-name hits only (``(name, source_id)`` pairs)."""
    hits = []
    for nm, src in names:
        for e in entries_by_core.get(core_name(nm), []):
            hits.append({"entry_id": e.entry_id, "name": nm, "source_id": src, "basis": "normalised name equality only"})
    return hits
