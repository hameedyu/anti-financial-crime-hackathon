"""FactExtractor: CaseBundle -> CaseFacts (Person 2 entry point)."""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from decimal import Decimal

from data_processing.models import CaseBundle, Transaction
from data_processing.serialize import to_jsonable
from data_processing.textutil import names_equal, round_amount
from . import metrics as M
from .document_matching import match_documents, table_coverage
from .documents import extract_document_facts
from .list_matching import assess_entry
from .models import SCHEMA_VERSION, AlertFacts, CaseFacts, EvidenceStatus
from .provenance import ProvenanceIndex, unresolved_citations
from .reviews import assess_reviews
from .rules import (ANALYSERS, Ctx, _doc_text, _fraud_indicators, _fraud_reports_matching, held_payment_requirements,
                    is_own_name, ownership_evidence, recurring_history, support_for, tm22, tx_brief)

_STATUS_ORDER = [EvidenceStatus.CONTRADICTED, EvidenceStatus.MISSING, EvidenceStatus.CLAIMED_ONLY,
                 EvidenceStatus.PARTIALLY_SUPPORTED, EvidenceStatus.VERIFIED, EvidenceStatus.UNKNOWN]


class FactExtractor:
    """Stateless; safe to reuse across cases of one or several institutions."""

    def extract(self, bundle: CaseBundle) -> CaseFacts:
        return extract_case_facts(bundle)


def extract_case_facts(bundle: CaseBundle) -> CaseFacts:
    cu = bundle.customer
    cname = cu.name if cu else None
    txs = [ct.transaction for ct in bundle.transactions]
    tx_map = {t.transaction_id: t for t in txs}
    window = bundle.case.review_window
    doc_facts = [extract_document_facts(d, cname) for d in bundle.evidence_documents]
    doc_by_id = {d["doc_id"]: d for d in doc_facts}

    alert_tids = bundle.alert_transaction_ids()
    focus = list(dict.fromkeys(alert_tids + ([bundle.held_payment.transaction_id] if bundle.held_payment else []) +
                               [t.transaction_id for t in txs if window.contains(t.booked_date) and not t.is_card]))
    groups: dict[str, list[Transaction]] = {}
    for a in bundle.alerts:
        for direction in ("in", "out"):
            g = [tx_map[t] for t in a.transaction_ids if t in tx_map and tx_map[t].direction == direction]
            if len(g) >= 2:
                groups[f"alert {a.alert_id} {direction}flows"] = g
    win_non_card = [t for t in txs if window.contains(t.booked_date) and not t.is_card]
    for key, g in sorted(M.group_by(win_non_card, lambda t: f"{M.counterparty_key(t)}|{t.direction}").items()):
        if len(g) >= 2:
            groups[f"window flows {key}"] = g
    matches = match_documents(doc_facts, txs, customer_name=cname, focus_ids=focus, groups=groups)
    matches_by_tx: dict[str, list] = defaultdict(list)
    for m in matches:
        for t in m.transaction_ids:
            matches_by_tx[t].append(m)

    list_entries = {}
    for c in bundle.internal_list_candidates:
        from data_processing.loaders import _build_list_entry
        list_entries[c["entry_id"]] = _build_list_entry(dict(c["entry"]), f"internal_list:{c['entry_id']}")

    ctx = Ctx(bundle=bundle, policy=bundle.policy, customer=cu, txs=txs, tx_map=tx_map, doc_facts=doc_facts,
              doc_by_id=doc_by_id, matches=matches, matches_by_tx=matches_by_tx, list_entries=list_entries)

    alert_facts: list[AlertFacts] = []
    list_matches = []
    for a in bundle.alerts:
        af = AlertFacts(alert_id=a.alert_id, rule_id=a.rule_id, rule_name=a.rule_name, message=a.message,
                        measured=dict(a.measured), transaction_ids=list(a.transaction_ids), recomputed={}, discrepancies=[],
                        observations={}, candidate_explanations=[], unresolved_requirements=[], coverage={},
                        risk_indicator_ids=[])
        if not bundle.policy.runs(a.rule_id):
            af.observations["policy_note"] = f"{a.rule_id} is not listed in this institution's POLICY.md"
        if a.rule_id == "TM22":
            tm22(ctx, a, af, list_matches)
        elif a.rule_id in ANALYSERS:
            ANALYSERS[a.rule_id](ctx, a, af)
        else:
            af.observations["note"] = f"no analyser for {a.rule_id}; supplied measurement preserved unchanged"
            af.coverage = {}
        alert_facts.append(af)

    # proactive screening hits not raised by an alert
    alerted_entries = {lm.entry_id for lm in list_matches}
    for c in bundle.internal_list_candidates:
        if c["entry_id"] in alerted_entries:
            continue
        e = list_entries[c["entry_id"]]
        rel = [t for t in txs if t.counterparty_name and names_equal(t.counterparty_name, e.name)
               and (window.contains(t.booked_date) or t.transaction_id in alert_tids)]
        cand = assess_entry(e, alert_ids=[], transactions=rel, alert_matched_on=None, doc_facts=doc_facts, customer_name=cname)
        cand.explanation = "Not alerted; found by name screening (" + "; ".join(c["included_because"]) + "). " + cand.explanation
        list_matches.append(cand)

    held_reqs = held_payment_requirements(ctx)
    _document_signal_indicators(ctx)
    _context_indicators(ctx, alert_tids)

    profile = _customer_profile(bundle, txs, doc_facts)
    tsum = _transaction_summary(bundle, txs)
    reviews = assess_reviews(bundle, txs, doc_facts)
    sof = _source_of_funds(ctx, alert_tids)

    supporting, contradicting = _explanations(ctx, list_matches)
    unresolved = []
    for af in alert_facts:
        for r in af.unresolved_requirements:
            if r.get("status") not in (EvidenceStatus.VERIFIED, "not_applicable", "evidence indicates a different party"):
                unresolved.append({"alert_id": af.alert_id, "rule_id": af.rule_id, **r})
    for r in held_reqs:
        if r.get("applies") and r["status"] != EvidenceStatus.VERIFIED:
            unresolved.append({"alert_id": None, "rule_id": None, **r})

    for d in doc_facts:
        cov = table_coverage(d, matches)
        if cov:
            d["table_reconciliation"] = cov

    index = ProvenanceIndex(bundle)
    dq_issues = [dataclasses.asdict(i) for i in bundle.data_quality]
    extraction_notes = []
    for d in doc_facts:
        if d["classification_source"] == "unknown_type":
            extraction_notes.append({"code": "unclassified_document_type", "doc_id": d["doc_id"], "type": d["type"]})
        if d["extraction"]["confidence"] in ("none", "low"):
            extraction_notes.append({"code": "low_extraction_confidence", "doc_id": d["doc_id"],
                                     "confidence": d["extraction"]["confidence"]})
    disc = [{"alert_id": af.alert_id, **x} for af in alert_facts for x in af.discrepancies]

    facts = CaseFacts(
        schema_version=SCHEMA_VERSION, case_id=bundle.case.case_id, institution=bundle.case.institution,
        customer_id=bundle.case.customer_id, alert_ids=[a.alert_id for a in bundle.alerts],
        held_payment_id=bundle.case.held_payment_id,
        review_window={"from": window.start, "to": window.end, "days": window.days},
        customer_profile=profile, transaction_summary=tsum, alert_facts=alert_facts, risk_indicators=ctx.indicators,
        supporting_explanations=supporting, contradicting_evidence=contradicting, document_facts=doc_facts,
        document_matches=matches, source_of_funds=sof, internal_list_matches=list_matches,
        previous_review_applicability=reviews, unresolved_questions=unresolved,
        data_quality={"bundle_issues": dq_issues, "extraction_notes": extraction_notes,
                      "measurement_discrepancies": disc, "held_payment_requirements": held_reqs,
                      "unresolved_citations": []},
        evidence_index=_evidence_index(bundle, index, doc_facts),
    )
    bad = unresolved_citations(to_jsonable(_strip_index(facts)), index)
    facts.data_quality["unresolved_citations"] = bad
    return facts


def _strip_index(f: CaseFacts) -> dict:
    d = {fld.name: getattr(f, fld.name) for fld in dataclasses.fields(f) if fld.name != "evidence_index"}
    return d


# --------------------------------------------------------------------------- sections
def _customer_profile(bundle: CaseBundle, txs: list[Transaction], doc_facts: list[dict]) -> dict:
    cu = bundle.customer
    if cu is None:
        return {"status": "missing_customer_record"}
    dec = cu.declared
    window = bundle.case.review_window
    win_in = M.total(t for t in txs if window.contains(t.booked_date) and t.direction == "in")
    expected_monthly = None
    basis = None
    if bundle.policy.get("TM01.baseline") == "annual_volume_monthly_share" and dec.annual_turnover_gbp is not None:
        expected_monthly, basis = M.money(dec.annual_turnover_gbp / 12), "policy: monthly share of expected annual payment volume"
    elif dec.monthly_income_gbp is not None:
        expected_monthly, basis = dec.monthly_income_gbp, "declared monthly income"
    elif dec.annual_turnover_gbp is not None:
        expected_monthly, basis = M.money(dec.annual_turnover_gbp / 12), "derived: declared annual turnover / 12 (labelled estimate)"
    kyc = next((d for d in doc_facts if d["type"] == "due_diligence_summary"), None)
    kyc_cmp = []
    if kyc:
        f = {k.lower(): v for k, v in kyc["fields"].items()}
        pairs = [("customer", cu.name), ("address", cu.address), ("occupation", cu.occupation), ("employer", cu.employer),
                 ("source of funds", dec.source_of_funds), ("account purpose", dec.account_purpose)]
        for k, v in pairs:
            if k in f and v is not None:
                kyc_cmp.append({"field": k, "kyc_document": f[k], "customer_record": v,
                                "outcome": "match" if f[k].strip().lower() == str(v).strip().lower() else "differs"})
        for k, v in (("declared monthly income", dec.monthly_income_gbp), ("expected annual payment volume", dec.annual_turnover_gbp)):
            if k in f and v is not None:
                amt = [a["value"] for a in kyc["amounts"] if a["label"] == k]
                kyc_cmp.append({"field": k, "kyc_document": amt[0] if amt else f[k], "customer_record": v,
                                "outcome": "match" if amt and amt[0] == v else "differs"})
    return {
        "customer_type": cu.kind, "name": cu.name, "business_activity": cu.business_activity, "occupation": cu.occupation,
        "employer": cu.employer, "account_purpose": dec.account_purpose, "declared_source_of_funds": dec.source_of_funds,
        "declared_monthly_income_gbp": dec.monthly_income_gbp, "declared_annual_turnover_gbp": dec.annual_turnover_gbp,
        "expected_monthly_volume_gbp": expected_monthly, "expected_monthly_volume_basis": basis,
        "expected_countries": list(dec.expected_countries), "expected_channels": list(dec.expected_channels),
        "risk_rating": cu.risk_rating, "onboarded_on": cu.onboarded_on, "account_ids": list(cu.account_ids),
        "profile_deviation": {"numerator_gbp": win_in, "denominator_gbp": expected_monthly,
                              "ratio": M.ratio(win_in, expected_monthly, "0.01"),
                              "method": f"all inflows in the review window {window.start}..{window.end} / expected monthly volume ({basis})"},
        "kyc_document": kyc["doc_id"] if kyc else None,
        "kyc_consistency": kyc_cmp,
    }


def _transaction_summary(bundle: CaseBundle, txs: list[Transaction]) -> dict:
    w = bundle.case.review_window
    win = [t for t in txs if w.contains(t.booked_date)]
    pre = [t for t in txs if t.booked_date < w.start]
    post = [t for t in txs if t.booked_date > w.end]
    pre_days = (w.start - min((t.booked_date for t in pre), default=w.start)).days
    pre_in = M.total(t for t in pre if t.direction == "in")
    pre_month = M.month_equivalent(pre_in, pre_days) if pre_days else None
    win_in = M.total(t for t in win if t.direction == "in")
    return {
        "window": M.summarize(win, label="review_window", start=w.start, end=w.end),
        "lookback": M.summarize(pre, label="before_window", start=min((t.booked_date for t in pre), default=None), end=w.start),
        "after_window": {"count": len(post), "transaction_ids": [t.transaction_id for t in post]},
        "history_coverage": {"earliest": min((t.booked_date for t in txs), default=None),
                             "latest": max((t.booked_date for t in txs), default=None),
                             "days_before_window": pre_days, "transaction_count": len(txs)},
        "historical_comparison": {
            "lookback_inflow_30_day_equivalent_gbp": pre_month, "window_inflow_gbp": win_in,
            "ratio_window_to_lookback": M.ratio(win_in, pre_month, "0.01"),
            "method": "lookback inflows scaled to 30 days (heuristic) compared with review-window inflows"},
        "chronology": [tx_brief(t) | {"alert_ids": list(ct.alert_ids), "held": ct.is_held_payment}
                       for ct in bundle.transactions if (ct.alert_ids or ct.is_held_payment or
                                                         (w.contains(ct.transaction.booked_date) and not ct.transaction.is_card))
                       for t in [ct.transaction]],
    }


def _source_of_funds(ctx: Ctx, alert_tids: list[str]) -> dict:
    """Evidence status of where alerted inflows (and the held payment's funding) came from."""
    inflows = [ctx.tx_map[t] for t in alert_tids if t in ctx.tx_map and ctx.tx_map[t].direction == "in"]
    per = []
    fraud_ids = {tid for i in ctx.indicators if i.indicator_type == "fraud_report_on_inflow" for tid in i.transaction_ids}
    for t in inflows:
        s = support_for(ctx, t.transaction_id)
        status = s["status"]
        supporting = list(s["doc_ids"])
        if is_own_name(ctx, t):
            oe = ownership_evidence(ctx, t)
            if _STATUS_ORDER.index(oe["status"]) > _STATUS_ORDER.index(status) or status == EvidenceStatus.MISSING:
                status = oe["status"]
                supporting = sorted(set(supporting) | set(oe["doc_ids"]))
        if t.transaction_id in fraud_ids:
            status = EvidenceStatus.CONTRADICTED
        if status == EvidenceStatus.MISSING:
            amount_text = f"£{t.amount_gbp:,.2f}"
            claims = [d["doc_id"] for d in ctx.doc_facts if d["reliability"] == "customer_statement" and (
                amount_text in _doc_text(ctx, d["doc_id"]) or
                (t.counterparty_name and t.counterparty_name.lower() in _doc_text(ctx, d["doc_id"]).lower()))]
            if claims:
                status = EvidenceStatus.CLAIMED_ONLY
                supporting = claims
        hist = recurring_history(ctx, t)
        per.append({"transaction_id": t.transaction_id, "amount_gbp": t.amount_gbp, "counterparty": t.counterparty_name,
                    "status": status, "supporting_evidence_ids": supporting,
                    "recurring_in_history": hist["recurring"], "prior_same_counterparty_ids": hist["prior_transaction_ids"]})
    total_in = sum((p["amount_gbp"] for p in per), Decimal(0))
    # materiality: a routine inflow (same payer seen at least twice before, no larger) worth under 10% of the
    # alerted inflows does not drive the overall status; it is still listed above
    for p in per:
        p["material"] = not (p["recurring_in_history"] and total_in and p["amount_gbp"] < total_in * Decimal("0.10"))
    material = [p for p in per if p["material"]]
    if not per:
        overall = EvidenceStatus.UNKNOWN
    else:
        sts = {p["status"] for p in (material or per)}
        if EvidenceStatus.CONTRADICTED in sts:
            overall = EvidenceStatus.CONTRADICTED
        elif sts == {EvidenceStatus.VERIFIED}:
            overall = EvidenceStatus.VERIFIED
        elif sts & {EvidenceStatus.VERIFIED, EvidenceStatus.PARTIALLY_SUPPORTED}:
            overall = EvidenceStatus.PARTIALLY_SUPPORTED
        elif EvidenceStatus.CLAIMED_ONLY in sts:
            overall = EvidenceStatus.CLAIMED_ONLY
        else:
            overall = EvidenceStatus.MISSING
    return {"status": overall, "declared_source_of_funds": ctx.customer.declared.source_of_funds if ctx.customer else None,
            "scope": "inflows cited by the case's alerts", "per_inflow": per,
            "materiality_rule": "routine inflows (payer seen 2+ times in the prior 120 days, amount not above 1.5x the largest) "
                                "under 10% of alerted inflows are excluded from the overall status",
            "material_unexplained_gbp": sum((p["amount_gbp"] for p in material if p["status"] not in (EvidenceStatus.VERIFIED,)), Decimal(0)),
            "supporting_evidence_ids": sorted({i for p in per for i in p["supporting_evidence_ids"]}),
            "note": "evidence status only; 'missing' means nothing on file addresses it, not that it is suspicious"}


def _document_signal_indicators(ctx: Ctx) -> None:
    done = {(i.indicator_type, tuple(i.supporting_ids)) for i in ctx.indicators}
    for d in ctx.doc_facts:
        for s in d["signals"]:
            if s["signal"] in ("threshold_inquiry", "intends_to_split_across_locations", "unwilling_to_explain"):
                continue  # added by the rule analysers when relevant
            if s["signal"] == "fraud_report":
                continue
            key = (f"document_signal:{s['signal']}", (d["doc_id"],))
            if key in done:
                continue
            done.add(key)
            ctx.indicator(indicator_type=key[0], rule_id=None, observed_value=s["quote"], threshold=None,
                          result=True if s["polarity"] == "risk" else None,
                          calculation=f"phrase '{s['phrase']}' in {d['doc_id']} ({d['type']}, {d['reliability']}); polarity {s['polarity']}",
                          supporting_ids=[d["doc_id"]], heuristic=True,
                          verification_status=(EvidenceStatus.CLAIMED_ONLY if d["reliability"] == "customer_statement"
                                               else EvidenceStatus.VERIFIED))
        if d.get("shared_addresses"):
            ctx.indicator(indicator_type="shared_payee_addresses", rule_id=None,
                          observed_value={a: len(n) for a, n in d["shared_addresses"].items()}, threshold=None, result=True,
                          calculation=f"{d['doc_id']}: {sum(len(n) for n in d['shared_addresses'].values())} listed people share "
                                      f"{len(d['shared_addresses'])} address(es)",
                          supporting_ids=[d["doc_id"]], verification_status=EvidenceStatus.VERIFIED,
                          caveats=["policy: shared contact details add context; they do not raise an alert on their own"])
        for dev in d.get("devices", []):
            others = [c for c in dev["customers"] if c != ctx.bundle.case.customer_id]
            expected = set(ctx.customer.declared.expected_countries) if ctx.customer else set()
            foreign = [c for c in dev["login_countries"] if c not in expected]
            if others or foreign:
                ctx.indicator(indicator_type="device_anomaly", rule_id=None, observed_value={"other_customers": others, "login_countries": foreign},
                              threshold=None, result=True if others or foreign else None,
                              calculation=f"{d['doc_id']}: device {dev['device']} used by other customers or from undeclared countries (context, not a trigger)",
                              supporting_ids=[d["doc_id"]], verification_status=EvidenceStatus.VERIFIED,
                              caveats=["policy: a shared device or IP adds context; it does not raise an alert on its own"])
    if any(d.get("fraud_report") for d in ctx.doc_facts):
        # all fraud reports, matched against all of the customer's inflows (TM04 may already have added some)
        _fraud_indicators(ctx, None, _fraud_reports_matching(ctx, []), None)


def _context_indicators(ctx: Ctx, alert_tids: list[str]) -> None:
    alerted = [ctx.tx_map[t] for t in alert_tids if t in ctx.tx_map]
    rounds = [t.transaction_id for t in alerted if round_amount(t.amount_gbp)]
    if rounds:
        ctx.indicator(indicator_type="round_amounts", rule_id=None, observed_value=len(rounds), threshold=None, result=None,
                      calculation="alerted transactions in whole hundreds of pounds", transaction_ids=rounds,
                      caveats=["policy: round amounts add context only"], verification_status=EvidenceStatus.VERIFIED)
    crypto = [t.transaction_id for t in ctx.txs if t.counterparty_name and ctx.bundle.case.review_window.contains(t.booked_date)
              and any(w in t.counterparty_name.lower() for w in ("crypto", "coin", "bitcoin", "exchange"))]
    if crypto:
        ctx.indicator(indicator_type="crypto_counterparty", rule_id=None, observed_value=len(crypto), threshold=None, result=None,
                      calculation="counterparty name contains crypto/coin/exchange (heuristic)", transaction_ids=crypto, heuristic=True,
                      caveats=["policy: crypto counterparties add context only"], verification_status=EvidenceStatus.VERIFIED)


def _explanations(ctx: Ctx, list_matches) -> tuple[list[dict], list[dict]]:
    sup, con = [], []
    for m in ctx.matches:
        item = {"kind": "document_match", "match_id": m.match_id, "doc_id": m.doc_id, "doc_type": m.doc_type,
                "transaction_ids": m.transaction_ids, "strength": m.strength, "status": m.verification_status,
                "reliability": m.reliability, "summary": m.explanation}
        if m.strength == "conflicting":
            con.append(item)
        elif m.strength in ("strong", "moderate"):
            sup.append(item)
    for lm in list_matches:
        item = {"kind": "internal_list", "entry_id": lm.entry_id, "confidence": lm.candidate_confidence,
                "control": lm.control_relationship.get("status"), "supporting_ids": lm.supporting_ids, "summary": lm.explanation}
        if lm.candidate_confidence in ("identifier_conflict", "address_premises_differ"):
            sup.append(item)
        elif lm.candidate_confidence in ("identity_corroborated", "probable") or lm.control_relationship.get("status") == "in_role_on_payment_date":
            con.append(item)
    for i in ctx.indicators:
        if i.result is True and i.indicator_type.startswith(("document_signal:", "fraud_report", "device_anomaly", "redemption_paid")):
            pol = "explanatory" if "polarity explanatory" in i.calculation else "risk"
            (sup if pol == "explanatory" else con).append({"kind": "indicator", "indicator_id": i.indicator_id,
                                                           "indicator_type": i.indicator_type, "supporting_ids": i.supporting_ids,
                                                           "transaction_ids": i.transaction_ids, "summary": i.calculation})
    return sup, con


def _evidence_index(bundle: CaseBundle, index: ProvenanceIndex, doc_facts: list[dict]) -> dict:
    tx = {ct.transaction.transaction_id: f"{ct.transaction.booked_at:%Y-%m-%d} {ct.transaction.direction} {ct.transaction.channel} "
          f"{ct.transaction.amount_gbp} {ct.transaction.counterparty_name or ct.transaction.location or ''}".strip()
          for ct in bundle.transactions if ct.alert_ids or ct.is_held_payment or
          (bundle.case.review_window.contains(ct.transaction.booked_date) and not ct.transaction.is_card)}
    return {
        "scope": "case",
        "documents": {d["doc_id"]: {"type": d["type"], "title": d["title"], "dated": d["dated"], "path": d["relative_path"],
                                    "reliability": d["reliability"]} for d in doc_facts},
        "transactions_described": tx,
        "reviews": {r.review_id: f"{r.reviewed_on} {r.rule_id} {r.verdict}" for r in bundle.previous_reviews},
        "list_entries": {c["entry_id"]: c["entry"].get("name") for c in bundle.internal_list_candidates},
        "alerts": {a.alert_id: a.rule_id for a in bundle.alerts},
        "all_ids": index.as_dict(),
        "submission_evidence_note": "transaction, document, list-entry and review IDs may be cited as evidence; alert IDs are for traceability",
    }
