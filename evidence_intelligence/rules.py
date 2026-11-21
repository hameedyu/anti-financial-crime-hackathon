"""Rule-specific fact builders.

One analyser per monitoring rule. Each recomputes the alert's measurement from
the transactions with the institution's own definition (from POLICY.md),
records any difference from the supplied ``measured`` values together with the
likely reason, and lists the evidence that could explain the alerted activity
and what is still unexplained. Analysers describe; they do not decide.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from data_processing.models import Alert, CaseBundle, Customer, PolicyParameters, Transaction
from data_processing.textutil import core_name, name_tokens, names_equal, normalize_name, parse_text_date, token_overlap
from . import metrics as M
from .list_matching import assess_entry
from .models import AlertFacts, DocumentMatch, EvidenceStatus, ListMatchCandidate, RiskIndicator

# Cities used to connect a document location to a higher-risk country. This is
# a small, documented gazetteer (heuristic); it is applied only to countries on
# the institution's own higher-risk list.
CITY_COUNTRY = {
    "beirut": "Lebanon", "baghdad": "Iraq", "basra": "Iraq", "erbil": "Iraq", "mosul": "Iraq",
    "port-au-prince": "Haiti", "damascus": "Syria", "aleppo": "Syria", "tehran": "Iran", "kabul": "Afghanistan",
    "caracas": "Venezuela", "minsk": "Belarus", "moscow": "Russia", "sanaa": "Yemen", "aden": "Yemen",
    "mogadishu": "Somalia", "kinshasa": "Democratic Republic of the Congo", "bamako": "Mali", "yangon": "Myanmar",
    "juba": "South Sudan", "pyongyang": "North Korea", "tripoli": None,  # ambiguous (Libya / Lebanon): never used
}

STRONG = {"strong"}
SUPPORTING = {"strong", "moderate"}


@dataclass
class Ctx:
    bundle: CaseBundle
    policy: PolicyParameters
    customer: Customer | None
    txs: list[Transaction]
    tx_map: dict[str, Transaction]
    doc_facts: list[dict]
    doc_by_id: dict[str, dict]
    matches: list[DocumentMatch]
    matches_by_tx: dict[str, list[DocumentMatch]]
    list_entries: dict[str, Any]
    indicators: list[RiskIndicator] = field(default_factory=list)

    def indicator(self, **kw) -> str:
        rid = f"RI{len(self.indicators) + 1:03d}"
        self.indicators.append(RiskIndicator(indicator_id=rid, **kw))
        return rid

    @property
    def customer_name(self) -> str | None:
        return self.customer.name if self.customer else None


# --------------------------------------------------------------------------- shared helpers
def as_of_date(alert: Alert, txs: list[Transaction]) -> tuple[date, str]:
    m = re.search(r"In the \d+ days to (\d{1,2} [A-Z][a-z]+ \d{4})", alert.message)
    if m and (d := parse_text_date(m.group(1))):
        return d, "window end stated in the alert message"
    if txs:
        return max(t.booked_date for t in txs), "latest alerted transaction date (message states no window)"
    return alert.created_at.date(), "alert creation date"


def _dec(v) -> Decimal | None:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int) and not isinstance(v, bool):
        return Decimal(v)
    return None


def discrepancy(name: str, supplied, recomputed, tolerance: Decimal, explanation: str) -> dict | None:
    s, r = _dec(supplied), _dec(recomputed)
    if s is None or r is None:
        return None
    if abs(s - r) <= tolerance:
        return None
    return {"metric": name, "supplied": s, "recomputed": r, "difference": r - s, "explanation": explanation}


def tx_brief(t: Transaction) -> dict:
    return {"transaction_id": t.transaction_id, "booked_at": t.booked_at, "direction": t.direction,
            "channel": t.channel, "amount_gbp": t.amount_gbp, "currency": t.currency, "amount": t.amount,
            "counterparty_name": t.counterparty_name, "counterparty_account": t.counterparty_account,
            "counterparty_country": t.counterparty_country, "reference": t.reference, "location": t.location}


def support_for(ctx: Ctx, tid: str) -> dict:
    """Best document support for one transaction."""
    ms = ctx.matches_by_tx.get(tid, [])
    order = {"strong": 0, "moderate": 1, "weak": 2, "conflicting": 3}
    if not ms:
        return {"transaction_id": tid, "status": EvidenceStatus.MISSING, "match_ids": [], "doc_ids": []}
    best = min(ms, key=lambda m: (order[m.strength], m.match_id))
    conflicts = [m for m in ms if m.strength == "conflicting"]
    status = best.verification_status
    if best.strength in ("weak",) and conflicts:
        status = EvidenceStatus.CONTRADICTED
    return {"transaction_id": tid, "status": status, "best_strength": best.strength, "best_match_id": best.match_id,
            "match_ids": [m.match_id for m in ms], "doc_ids": sorted({m.doc_id for m in ms}),
            "conflicting_doc_ids": sorted({m.doc_id for m in conflicts})}


def coverage(ctx: Ctx, tids: list[str]) -> dict:
    rows = [support_for(ctx, t) for t in tids if t in ctx.tx_map]
    amt = lambda ids: sum((ctx.tx_map[i].amount_gbp for i in ids), M.ZERO)
    by_status: dict[str, list[str]] = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r["transaction_id"])
    tot = amt([r["transaction_id"] for r in rows])
    ver = amt(by_status.get(EvidenceStatus.VERIFIED, []))
    return {"transactions": len(rows), "total_gbp": tot,
            "by_status": {k: {"count": len(v), "total_gbp": amt(v), "transaction_ids": v} for k, v in sorted(by_status.items())},
            "verified_share_of_amount": M.ratio(ver, tot), "per_transaction": rows}


def explanations_for(ctx: Ctx, tids: list[str]) -> list[dict]:
    out = []
    seen = set()
    for tid in tids:
        for m in ctx.matches_by_tx.get(tid, []):
            if m.match_id in seen:
                continue
            seen.add(m.match_id)
            d = ctx.doc_by_id.get(m.doc_id, {})
            out.append({"match_id": m.match_id, "doc_id": m.doc_id, "doc_type": m.doc_type, "title": d.get("title"),
                        "reliability": m.reliability, "strength": m.strength, "status": m.verification_status,
                        "transaction_ids": m.transaction_ids, "summary": m.explanation})
    return out


def docs_with_signal(ctx: Ctx, signal: str) -> list[tuple[dict, dict]]:
    return [(d, s) for d in ctx.doc_facts for s in d["signals"] if s["signal"] == signal]


def is_own_name(ctx: Ctx, t: Transaction) -> bool:
    return bool(ctx.customer_name and t.counterparty_name and names_equal(t.counterparty_name, ctx.customer_name))


def recurring_history(ctx: Ctx, t: Transaction, lookback_days: int = 120) -> dict:
    """Earlier inflows (or outflows) with the same counterparty and direction: a routine-pattern fact."""
    k = M.counterparty_key(t)
    prior = [x for x in ctx.txs if x.direction == t.direction and x.booked_at < t.booked_at
             and (t.booked_date - x.booked_date).days <= lookback_days and M.counterparty_key(x) == k and not x.is_card]
    amounts = [x.amount_gbp for x in prior]
    return {"prior_count": len(prior), "prior_transaction_ids": [x.transaction_id for x in prior],
            "prior_max_gbp": max(amounts) if amounts else None,
            "recurring": len(prior) >= 2 and t.amount_gbp <= (max(amounts) * Decimal("1.5"))}


def ownership_evidence(ctx: Ctx, t: Transaction) -> dict:
    """Is the counterparty account of ``t`` shown to be the customer's own?

    ``verified`` needs a third-party document (e.g. a bank statement) that names
    the customer as holder *and* ties to this transaction (same account digits,
    or a statement entry with the same amount and date). A document naming the
    customer as holder without that tie is ``partially_supported``; the
    customer's own word is ``claimed_only``; a name match alone is ``missing``.
    """
    from data_processing.textutil import find_account_refs
    trefs = find_account_refs(t.counterparty_account or "")
    best = {"status": EvidenceStatus.MISSING, "doc_ids": [], "detail": "no document on file shows the customer holds the other account"}
    for d in ctx.doc_facts:
        for o in d.get("ownership", []):
            holder = o.get("holder")
            if not holder or not ctx.customer_name or not names_equal(holder, ctx.customer_name):
                continue
            tied, how = False, None
            for a in o.get("account_refs", []):
                for r in trefs:
                    if a.get("last_digits") and a["last_digits"] == r["last_digits"]:
                        tied, how = True, f"account digits {a['last_digits']} match"
            for row in d["rows"]:
                if row.get("date") and any(x["value"] == t.amount_gbp for x in row.get("amounts", [])) \
                        and abs((row["date"] - t.booked_date).days) <= 3:
                    tied, how = True, f"statement entry {row['date']} for {t.amount_gbp}"
            if d["reliability"] == "customer_statement":
                cand = {"status": EvidenceStatus.CLAIMED_ONLY, "doc_ids": [d["doc_id"]],
                        "detail": f"customer states the account is theirs ({d['doc_id']})"}
            elif tied:
                cand = {"status": EvidenceStatus.VERIFIED, "doc_ids": [d["doc_id"]],
                        "detail": f"{d['doc_id']} names {holder} as account holder; {how}"}
            else:
                cand = {"status": EvidenceStatus.PARTIALLY_SUPPORTED, "doc_ids": [d["doc_id"]],
                        "detail": f"{d['doc_id']} names {holder} as holder of an account, but nothing ties it to this transaction"}
            rank = [EvidenceStatus.VERIFIED, EvidenceStatus.PARTIALLY_SUPPORTED, EvidenceStatus.CLAIMED_ONLY, EvidenceStatus.MISSING]
            if rank.index(cand["status"]) < rank.index(best["status"]):
                best = cand
    return best


def _baseline(ctx: Ctx) -> tuple[Decimal | None, str]:
    cu = ctx.customer
    if cu is None:
        return None, "customer record missing"
    dec = cu.declared
    if ctx.policy.get("TM01.baseline") == "annual_volume_monthly_share":
        if dec.annual_turnover_gbp is None:
            return None, "no declared annual payment volume"
        return M.money(dec.annual_turnover_gbp / 12), "monthly share (1/12) of the declared expected annual payment volume, per POLICY.md"
    if cu.kind == "individual":
        if dec.monthly_income_gbp is None:
            return None, "no declared monthly income"
        return dec.monthly_income_gbp, "declared monthly income"
    if dec.annual_turnover_gbp is not None:
        return M.money(dec.annual_turnover_gbp / 12), ("declared annual turnover / 12 (POLICY.md compares with monthly turnover; "
                                                       "the customer record holds annual turnover)")
    return None, "no declared turnover"


# --------------------------------------------------------------------------- analysers
def tm01(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    days = int(a.measured.get("window_days", 30))
    end, how = as_of_date(a, [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map])
    start, end = M.window_ending(end, days)
    inflows = [t for t in ctx.txs if t.direction == "in" and M.in_dates(t, start, end) and t.channel != "fund_redemption"]
    received = M.total(inflows)
    base, base_how = _baseline(ctx)
    mult = M.ratio(received, base, "0.01")
    limit = ctx.policy.get("TM01.business_multiple") if (ctx.customer and ctx.customer.kind == "business") else ctx.policy.get("TM01.individual_multiple")
    own = [t for t in inflows if is_own_name(ctx, t)]
    own_total = M.total(own)
    own_status = [{"transaction_id": t.transaction_id, "amount_gbp": t.amount_gbp, **ownership_evidence(ctx, t),
                   "history": recurring_history(ctx, t)} for t in own]
    breakdown = []
    for k, grp in sorted(M.group_by(inflows, M.counterparty_key).items(), key=lambda kv: (-M.total(kv[1]), kv[0])):
        breakdown.append({"counterparty": grp[0].counterparty_name or grp[0].location or grp[0].channel,
                          "count": len(grp), "total_gbp": M.total(grp), "share_of_received": M.ratio(M.total(grp), received),
                          "own_name": is_own_name(ctx, grp[0]), "channels": sorted({t.channel for t in grp}),
                          "transaction_ids": [t.transaction_id for t in grp],
                          "support": [support_for(ctx, t.transaction_id)["status"] for t in grp]})
    rfi_mult = ctx.policy.get("own_account.rfi_multiple")
    af.recomputed = {"window": {"from": start, "to": end, "days": days, "end_basis": how},
                     "received_gbp": received, "baseline_gbp": base, "baseline_basis": base_how,
                     "multiple": mult, "limit_multiple": limit,
                     "inflow_transaction_ids": [t.transaction_id for t in inflows]}
    af.discrepancies = [d for d in (
        discrepancy("received_gbp", a.measured.get("received_gbp"), received, Decimal("0.01"),
                    "recomputed over inbound transactions dated in the window (fund redemptions excluded); a difference "
                    "points to a different window boundary or inflow definition in the monitoring system"),
        discrepancy("baseline_gbp", a.measured.get("baseline_gbp"), base, Decimal("0.01"), base_how),
        discrepancy("multiple", a.measured.get("multiple"), mult, Decimal("0.02"), "follows from the figures above"),
    ) if d]
    excl_mult = M.ratio(received - own_total, base, "0.01")
    af.observations = {
        "inflow_breakdown": breakdown,
        "own_name_inflows": {"total_gbp": own_total, "transaction_ids": [t.transaction_id for t in own],
                             "ownership_evidence": own_status},
        "multiple_excluding_own_name_inflows": excl_mult,
        "own_account_policy": {"rfi_multiple": rfi_mult, "activity_multiple": mult,
                               "activity_at_or_above_rfi_multiple": (mult >= rfi_mult) if (mult is not None and rfi_mult is not None) else None,
                               "rule_text": ctx.policy.params.get("own_account.rfi_multiple").evidence if ctx.policy.params.get("own_account.rfi_multiple") else None},
    }
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="profile_multiple", rule_id="TM01", observed_value=mult, threshold=limit,
        result=None if mult is None or limit is None else mult > limit,
        calculation=f"inflows {received} over {start}..{end} / baseline {base} ({base_how})",
        transaction_ids=[t.transaction_id for t in inflows], alert_ids=[a.alert_id],
        verification_status=EvidenceStatus.VERIFIED if base is not None else EvidenceStatus.UNKNOWN))
    if own:
        verified = all(o["status"] == EvidenceStatus.VERIFIED for o in own_status)
        af.risk_indicator_ids.append(ctx.indicator(
            indicator_type="own_account_activity_multiple", rule_id="TM01", observed_value=mult, threshold=rfi_mult,
            result=None if mult is None or rfi_mult is None else mult >= rfi_mult,
            calculation=f"total activity multiple {mult} compared with the own-account RFI multiple {rfi_mult} in POLICY.md; "
                        f"own-name inflows {own_total}",
            transaction_ids=[t.transaction_id for t in own], alert_ids=[a.alert_id],
            supporting_ids=sorted({d for o in own_status for d in o["doc_ids"]}),
            caveats=[] if verified else ["at least one own-name inflow is not shown by a document to come from the customer's own account"],
            verification_status=EvidenceStatus.VERIFIED if verified else EvidenceStatus.PARTIALLY_SUPPORTED))
    ids = [t.transaction_id for t in inflows]
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    for b in breakdown:
        if b["own_name"]:
            continue
        unsupported = [tid for tid, s in zip(b["transaction_ids"], b["support"]) if s in (EvidenceStatus.MISSING, EvidenceStatus.CLAIMED_ONLY, EvidenceStatus.CONTRADICTED)]
        if unsupported and base is not None and M.total([ctx.tx_map[t] for t in unsupported]) >= base:
            af.unresolved_requirements.append({
                "requirement": "documented source of inflows above the declared baseline",
                "status": "unresolved", "detail": f"{len(unsupported)} inflow(s) from {b['counterparty']} totalling "
                f"{M.total([ctx.tx_map[t] for t in unsupported])} have no supporting document", "transaction_ids": unsupported})
    for o in own_status:
        routine = o["history"]["recurring"] and (base is None or o["amount_gbp"] < base)
        if o["status"] != EvidenceStatus.VERIFIED and not routine:
            af.unresolved_requirements.append({"requirement": "document showing the customer holds the sending account",
                                               "status": o["status"], "detail": o["detail"], "transaction_ids": [o["transaction_id"]]})


def tm02(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    p = ctx.policy
    days = int(a.measured.get("window_days", p.get("TM02.window_days", 10)))
    end, how = as_of_date(a, [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map])
    start, end = M.window_ending(end, days)
    if p.get("TM02.withdrawal_days"):
        r = M.rapid_movement_days_investment(ctx.txs, start, end, int(p.get("TM02.withdrawal_days")))
        received, paid, share, invested = r["received_gbp"], r["withdrawn_gbp"], r["withdrawn_share"], r["invested_gbp"]
        in_ids, out_ids = r["deposit_ids"], r["withdrawal_ids"]
    else:
        flt = (lambda t: not is_own_name(ctx, t)) if p.get("TM02.excludes_client_funding") else (lambda t: True)
        r = M.rapid_movement_hours(ctx.txs, start, end, int(p.get("TM02.payout_hours") or 48),
                                   inflow_filter=flt, exclude_card=bool(p.get("TM02.excludes_card", True)))
        received, paid, share, invested = r["received_gbp"], r["outflows_within_hours_gbp"], r["share_capped"], None
        in_ids, out_ids = r["inflow_ids"], r["outflow_ids"]
    af.recomputed = {"window": {"from": start, "to": end, "days": days, "end_basis": how}, **r,
                     "policy": {"min_share": p.get("TM02.min_share"), "min_received_gbp": p.get("TM02.min_received_gbp"),
                                "payout_hours": p.get("TM02.payout_hours"), "withdrawal_days": p.get("TM02.withdrawal_days"),
                                "max_invested_share": p.get("TM02.max_invested_share"),
                                "client_funding_excluded": p.get("TM02.excludes_client_funding")}}
    af.discrepancies = [d for d in (
        discrepancy("received_gbp", a.measured.get("received_gbp"), received, Decimal("0.01"),
                    "inflows in the recomputed window (own funding excluded where the policy says so)"),
        discrepancy("paid_out_gbp", a.measured.get("paid_out_gbp"), paid, Decimal("0.01"),
                    "outflows inside the policy horizon after an inflow; the monitoring system may bound the horizon differently"),
        discrepancy("invested_gbp", a.measured.get("invested_gbp"), invested, Decimal("0.01"), "fund purchases dated in the window"),
    ) if d]
    ins = [ctx.tx_map[i] for i in in_ids]
    outs = [ctx.tx_map[i] for i in out_ids]
    third_party_in = [t for t in ins if not is_own_name(ctx, t) and not t.is_cash]
    dest_countries = sorted({t.counterparty_country for t in outs if t.counterparty_country})
    af.observations = {
        "inflows": [tx_brief(t) for t in ins], "outflows": [tx_brief(t) for t in outs],
        "distinct_payers": len({M.counterparty_key(t) for t in ins}), "distinct_payees": len({M.counterparty_key(t) for t in outs}),
        "third_party_inflow_ids": [t.transaction_id for t in third_party_in],
        "outflow_destination_countries": dest_countries,
        "outflows_to_own_name": [t.transaction_id for t in outs if is_own_name(ctx, t)],
    }
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="rapid_outflow_share", rule_id="TM02", observed_value=share, threshold=p.get("TM02.min_share"),
        result=None if share is None else (share >= p.get("TM02.min_share") and received >= p.get("TM02.min_received_gbp")),
        calculation=r["method"], transaction_ids=in_ids + out_ids, alert_ids=[a.alert_id],
        caveats=["pass-through arithmetic only; it does not show which inflow funded which payment"],
        verification_status=EvidenceStatus.VERIFIED))
    if p.get("TM02.max_invested_share") is not None:
        inv_share = M.ratio(invested or M.ZERO, received)
        af.risk_indicator_ids.append(ctx.indicator(
            indicator_type="little_invested", rule_id="TM02", observed_value=inv_share, threshold=p.get("TM02.max_invested_share"),
            result=None if inv_share is None else inv_share < p.get("TM02.max_invested_share"),
            calculation="fund purchases / deposits in the window", transaction_ids=r.get("investment_ids", []),
            alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED))
    ids = in_ids + out_ids
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    unsupported_in = [t.transaction_id for t in third_party_in if support_for(ctx, t.transaction_id)["status"] in
                      (EvidenceStatus.MISSING, EvidenceStatus.CLAIMED_ONLY)]
    if unsupported_in:
        af.unresolved_requirements.append({"requirement": "who sent the inflows that were paid on, and why",
                                           "status": "unresolved", "detail": f"{len(unsupported_in)} third-party inflow(s) without a supporting document",
                                           "transaction_ids": unsupported_in})


def _tm03_subject(ctx: Ctx, t: Transaction) -> bool:
    lo, hi = ctx.policy.get("TM03.band_low_gbp"), ctx.policy.get("TM03.band_high_gbp")
    subj = ctx.policy.get("TM03.subject")
    if not (lo <= t.amount_gbp <= hi):
        return False
    if subj == "cash_deposit":
        return t.channel == "cash_deposit"
    if subj == "cash_topup":
        return t.channel == "cash_topup"
    return t.direction == "out" and not t.is_card


def tm03(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    p = ctx.policy
    days = int(a.measured.get("window_days", p.get("TM03.window_days", 10)))
    end, how = as_of_date(a, [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map])
    start, end = M.window_ending(end, days)
    qual = [t for t in ctx.txs if _tm03_subject(ctx, t) and M.in_dates(t, start, end)]
    if p.get("TM03.subject") == "payment_same_beneficiary":
        groups = M.group_by(qual, M.counterparty_key)
    else:
        groups = {"all": qual}
    clusters = []
    for k, g in sorted(groups.items()):
        clusters += M.clusters(g, days, int(p.get("TM03.min_count")))
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    alerted_count = len(alerted)
    af.recomputed = {"window": {"from": start, "to": end, "days": days, "end_basis": how},
                     "band": [p.get("TM03.band_low_gbp"), p.get("TM03.band_high_gbp")], "subject": p.get("TM03.subject"),
                     "min_count": p.get("TM03.min_count"), "qualifying_transaction_ids": [t.transaction_id for t in qual],
                     "count": len(qual), "total_gbp": M.total(qual), "clusters": clusters,
                     "locations": len({t.location for t in qual if t.location})}
    af.discrepancies = [d for d in (
        discrepancy("count", a.measured.get("count", a.measured.get("payments")), Decimal(alerted_count), Decimal(0),
                    "count of transactions cited by the alert"),
        discrepancy("total_gbp", a.measured.get("total_gbp"), M.total(alerted), Decimal("0.01"), "sum of the alerted transactions"),
    ) if d]
    # nearby transactions just above the band and cash split across locations
    hi = p.get("TM03.band_high_gbp")
    above = [t for t in ctx.txs if M.in_dates(t, start, end) and t.amount_gbp > hi and _same_kind(ctx, t)]
    thr = p.get("invoice_required.min_gbp")
    af.observations = {
        "alerted_transactions": [tx_brief(t) for t in alerted],
        "same_kind_above_band_in_window": [t.transaction_id for t in above],
        "amounts_are_round": [t.transaction_id for t in alerted if t.amount_gbp == t.amount_gbp.to_integral_value()],
        "invoice_threshold_gbp": thr,
        "note": "Amounts below a threshold are not on their own evidence of deliberate splitting.",
    }
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="sub_threshold_cluster", rule_id="TM03", observed_value=len(qual), threshold=p.get("TM03.min_count"),
        result=len(qual) >= int(p.get("TM03.min_count")),
        calculation=f"{p.get('TM03.subject')} each between {p.get('TM03.band_low_gbp')} and {hi} within {days} days to {end}",
        transaction_ids=[t.transaction_id for t in qual], alert_ids=[a.alert_id],
        caveats=["descriptive pattern; intent cannot be inferred from amounts alone"], verification_status=EvidenceStatus.VERIFIED))
    for d, s in docs_with_signal(ctx, "threshold_inquiry") + docs_with_signal(ctx, "intends_to_split_across_locations"):
        af.risk_indicator_ids.append(ctx.indicator(
            indicator_type=f"document_signal:{s['signal']}", rule_id="TM03", observed_value=s["quote"], threshold=None,
            result=True, calculation=f"phrase '{s['phrase']}' in {d['doc_id']} ({d['type']})", supporting_ids=[d["doc_id"]],
            alert_ids=[a.alert_id], heuristic=True,
            verification_status=EvidenceStatus.VERIFIED if d["reliability"] == "institution_record" else EvidenceStatus.CLAIMED_ONLY))
    ids = [t.transaction_id for t in alerted]
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    unexplained = [r["transaction_id"] for r in af.coverage["per_transaction"] if r["status"] != EvidenceStatus.VERIFIED]
    if unexplained:
        af.unresolved_requirements.append({"requirement": "records showing where each sub-threshold transaction came from or went",
                                           "status": "unresolved", "detail": f"{len(unexplained)} of {len(ids)} alerted transactions lack a verified record",
                                           "transaction_ids": unexplained})


def _same_kind(ctx: Ctx, t: Transaction) -> bool:
    subj = ctx.policy.get("TM03.subject")
    return (subj == "cash_deposit" and t.channel == "cash_deposit") or (subj == "cash_topup" and t.channel == "cash_topup") \
        or (subj == "payment_same_beneficiary" and t.direction == "out" and not t.is_card)


def tm04(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    p = ctx.policy
    days = int(a.measured.get("window_days", p.get("TM04.window_days", 30)))
    end, how = as_of_date(a, [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map])
    start, end = M.window_ending(end, days)
    direction = {"incoming": "in", "outgoing": "out"}.get(str(a.measured.get("direction")), "out" if p.get("TM04.scope") == "beneficiaries" else "in")
    flows = [t for t in ctx.txs if t.direction == direction and M.in_dates(t, start, end) and not t.is_card and not t.is_cash
             and t.counterparty_name and not is_own_name(ctx, t)]
    by_cp = M.group_by(flows, M.counterparty_key)
    by_acct = {t.counterparty_account for t in flows if t.counterparty_account}
    named_in_docs: dict[str, list[str]] = {}
    for d in ctx.doc_facts:
        names = [r["name"] for r in d["rows"] if r.get("name")] + [x["name"] for x in d["parties"]]
        for k, g in by_cp.items():
            if any(names_equal(n, g[0].counterparty_name) for n in names):
                named_in_docs.setdefault(k, []).append(d["doc_id"])
    cps = []
    for k, g in sorted(by_cp.items(), key=lambda kv: (-M.total(kv[1]), kv[0])):
        cps.append({"counterparty": g[0].counterparty_name, "count": len(g), "total_gbp": M.total(g),
                    "accounts": sorted({t.counterparty_account for t in g if t.counterparty_account}),
                    "countries": sorted({t.counterparty_country for t in g if t.counterparty_country}),
                    "references": sorted({t.reference for t in g if t.reference}),
                    "named_in_documents": sorted(set(named_in_docs.get(k, []))),
                    "transaction_ids": [t.transaction_id for t in g]})
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    af.recomputed = {"window": {"from": start, "to": end, "days": days, "end_basis": how}, "direction": direction,
                     "distinct_counterparties_by_name": len(by_cp), "distinct_counterparty_accounts": len(by_acct),
                     "total_gbp": M.total(flows), "limit": p.get("TM04.max_counterparties"),
                     "transaction_ids": [t.transaction_id for t in flows],
                     "normalisation": "names lower-cased, punctuation and legal suffixes removed; own-name transfers excluded"}
    af.discrepancies = [d for d in (
        discrepancy("distinct_counterparties", a.measured.get("distinct_counterparties"), Decimal(len(by_cp)), Decimal(0),
                    "recomputed with conservative name normalisation over non-card, non-cash flows in the window"),
        discrepancy("total_gbp", a.measured.get("total_gbp"), M.total(alerted), Decimal("0.01"), "sum of the alerted transactions"),
    ) if d]
    explained = sum(1 for c in cps if c["named_in_documents"])
    fraud = _fraud_reports_matching(ctx, flows)
    af.observations = {"counterparties": cps, "counterparties_named_in_documents": explained,
                       "share_of_counterparties_named_in_documents": M.ratio(Decimal(explained), Decimal(len(cps))) if cps else None,
                       "fraud_reports_on_these_flows": fraud,
                       "references_used": sorted({t.reference for t in flows if t.reference})}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="many_counterparties", rule_id="TM04", observed_value=len(by_cp), threshold=p.get("TM04.max_counterparties"),
        result=len(by_cp) > int(p.get("TM04.max_counterparties")),
        calculation=f"distinct {('payers' if direction == 'in' else 'payees')} {start}..{end}",
        transaction_ids=[t.transaction_id for t in flows], alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED))
    _fraud_indicators(ctx, a, fraud, "TM04")
    ids = [t.transaction_id for t in alerted]
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    af.coverage["counterparties_named_in_documents"] = {"named": explained, "total": len(cps)}
    if explained < len(cps):
        af.unresolved_requirements.append({"requirement": "who the counterparties are and what the payments were for",
                                           "status": "partially_supported" if explained else "missing",
                                           "detail": f"{len(cps) - explained} of {len(cps)} counterparties are not named in any document"})


def _fraud_reports_matching(ctx: Ctx, txs: list[Transaction]) -> list[dict]:
    out = []
    for d in ctx.doc_facts:
        fr = d.get("fraud_report")
        if not fr:
            continue
        hits = []
        for t in txs if txs else ctx.txs:
            ok_amt = fr.get("amount") is not None and t.amount_gbp == fr["amount"]
            ok_name = fr.get("victim") and t.counterparty_name and names_equal(fr["victim"], t.counterparty_name)
            ok_date = fr.get("sent_on") and abs((t.booked_date - fr["sent_on"]).days) <= 2
            if t.direction == "in" and ok_amt and (ok_name or ok_date):
                hits.append({"transaction_id": t.transaction_id, "amount": ok_amt, "payer_name": bool(ok_name), "date": bool(ok_date)})
        out.append({"doc_id": d["doc_id"], "victim": fr.get("victim"), "amount": fr.get("amount"), "sent_on": fr.get("sent_on"),
                    "reporting_bank": fr.get("reporting_bank"), "matched_transactions": hits})
    return out


def _fraud_indicators(ctx: Ctx, a: Alert | None, fraud: list[dict], rule: str | None) -> None:
    for f in fraud:
        if any(i.indicator_type == "fraud_report_on_inflow" and f["doc_id"] in i.supporting_ids for i in ctx.indicators):
            continue
        ctx.indicator(indicator_type="fraud_report_on_inflow", rule_id=rule, observed_value=f["amount"], threshold=None,
                      result=bool(f["matched_transactions"]) or None,
                      calculation=f"{f['doc_id']}: {f['reporting_bank']} reports its customer {f['victim']} sent {f['amount']} on "
                                  f"{f['sent_on']} as a scam; matched to inbound transactions by amount and payer/date",
                      transaction_ids=[h["transaction_id"] for h in f["matched_transactions"]], supporting_ids=[f["doc_id"]],
                      alert_ids=[a.alert_id] if a else [], verification_status=EvidenceStatus.VERIFIED if f["matched_transactions"] else EvidenceStatus.PARTIALLY_SUPPORTED,
                      caveats=[] if f["matched_transactions"] else ["no inbound transaction matched the report's amount with its payer or date"])


def tm05(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    kind = ctx.customer.kind if ctx.customer else "individual"
    limit = ctx.policy.get("TM05.business_gbp") if kind == "business" else ctx.policy.get("TM05.individual_gbp")
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    cash = [t for t in alerted if t.is_cash]
    big = [t for t in cash if t.amount_gbp >= limit]
    af.recomputed = {"limit_gbp": limit, "customer_kind": kind, "cash_transaction_ids": [t.transaction_id for t in cash],
                     "at_or_above_limit_ids": [t.transaction_id for t in big],
                     "largest_cash_gbp": max((t.amount_gbp for t in cash), default=None)}
    af.discrepancies = [d for d in (
        discrepancy("amount_gbp", a.measured.get("amount_gbp"), max((t.amount_gbp for t in cash), default=None), Decimal("0.01"), "largest alerted cash amount"),
        discrepancy("limit_gbp", a.measured.get("limit_gbp"), limit, Decimal("0.01"), f"POLICY.md limit for a {kind} customer"),
    ) if d]
    sales = [d for d in ctx.doc_facts if d.get("paid_in_cash")]
    af.observations = {"cash_transactions": [tx_brief(t) for t in cash],
                       "documents_describing_cash_payment": [d["doc_id"] for d in sales],
                       "non_cash_alerted": [t.transaction_id for t in alerted if not t.is_cash]}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="large_cash", rule_id="TM05", observed_value=max((t.amount_gbp for t in cash), default=None), threshold=limit,
        result=bool(big), calculation=f"cash transactions at or above {limit} ({kind})",
        transaction_ids=[t.transaction_id for t in cash], alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED))
    ids = [t.transaction_id for t in alerted]
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    for r in af.coverage["per_transaction"]:
        if r["status"] != EvidenceStatus.VERIFIED:
            af.unresolved_requirements.append({"requirement": "documented source of the cash", "status": r["status"],
                                               "transaction_ids": [r["transaction_id"]],
                                               "detail": "no third-party document reconciles to this cash amount" if r["status"] == EvidenceStatus.MISSING
                                               else f"best support: {r.get('best_strength')} ({', '.join(r['doc_ids'])})"})


def tm06(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    p = ctx.policy
    days = int(a.measured.get("window_days", p.get("TM06.window_days", 30)))
    end, how = as_of_date(a, [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map])
    start, end = M.window_ending(end, days)
    hr = set(p.high_risk_countries)
    expected = set(ctx.customer.declared.expected_countries) if ctx.customer else set()
    flows = [t for t in ctx.txs if M.in_dates(t, start, end) and t.counterparty_country in hr and t.counterparty_country not in expected]
    by_country = {c: {"total_gbp": M.total(g), "count": len(g), "in_gbp": M.total(x for x in g if x.direction == "in"),
                      "out_gbp": M.total(x for x in g if x.direction == "out"), "transaction_ids": [t.transaction_id for t in g],
                      "counterparties": sorted({t.counterparty_name for t in g if t.counterparty_name})}
                  for c, g in sorted(M.group_by(flows, lambda t: t.counterparty_country).items())}
    mentions = []
    for d in ctx.doc_facts:
        full = _doc_text(ctx, d["doc_id"]).lower()
        for c in by_country:
            if re.search(rf"\b{re.escape(c.lower())}\b", full):
                mentions.append({"doc_id": d["doc_id"], "country": c, "basis": "country named in the document"})
            else:
                for city, cc in CITY_COUNTRY.items():
                    if cc == c and re.search(rf"\b{re.escape(city)}\b", _doc_text(ctx, d["doc_id"]).lower()):
                        mentions.append({"doc_id": d["doc_id"], "country": c, "basis": f"city '{city}' named (gazetteer, heuristic)"})
                        break
    total_hr = M.total(flows)
    af.recomputed = {"window": {"from": start, "to": end, "days": days, "end_basis": how}, "by_country": by_country,
                     "total_gbp": total_hr, "limit_gbp": p.get("TM06.min_total_gbp"), "declared_countries": sorted(expected),
                     "high_risk_list_as_of": p.high_risk_as_of}
    af.discrepancies = [d for d in (
        discrepancy("total_gbp", a.measured.get("total_gbp"), total_hr, Decimal("0.01"),
                    "all transactions in the window to/from undeclared higher-risk countries"),
    ) if d]
    af.observations = {"documents_mentioning_corridor": mentions,
                       "countries_in_alert": a.measured.get("countries"),
                       "country_on_policy_list": {c: c in hr for c in by_country}}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="undeclared_high_risk_corridor", rule_id="TM06", observed_value=total_hr, threshold=p.get("TM06.min_total_gbp"),
        result=total_hr >= p.get("TM06.min_total_gbp") if flows else False,
        calculation=f"flows {start}..{end} with countries on the POLICY.md higher-risk list ({p.high_risk_as_of}) not declared by the customer",
        transaction_ids=[t.transaction_id for t in flows], alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED))
    for d, s in docs_with_signal(ctx, "unwilling_to_explain"):
        af.risk_indicator_ids.append(ctx.indicator(
            indicator_type="document_signal:unwilling_to_explain", rule_id="TM06", observed_value=s["quote"], threshold=None, result=True,
            calculation=f"phrase '{s['phrase']}' in {d['doc_id']}", supporting_ids=[d["doc_id"]], alert_ids=[a.alert_id], heuristic=True,
            verification_status=EvidenceStatus.VERIFIED if d["reliability"] == "institution_record" else EvidenceStatus.CLAIMED_ONLY))
    ids = [t.transaction_id for t in flows] or list(a.transaction_ids)
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    unexplained = [r["transaction_id"] for r in af.coverage["per_transaction"] if r["status"] not in (EvidenceStatus.VERIFIED,)]
    if unexplained:
        af.unresolved_requirements.append({"requirement": "documents showing the purpose of the higher-risk corridor payments",
                                           "status": "unresolved", "transaction_ids": unexplained,
                                           "detail": f"{len(unexplained)} corridor transaction(s) without a verified document"})


def _doc_text(ctx: Ctx, doc_id: str) -> str:
    for d in ctx.bundle.evidence_documents:
        if d.doc_id == doc_id:
            return d.text or ""
    return ""


def tm07(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    first = min((t.booked_date for t in alerted), default=a.created_at.date())
    dorm = M.dormancy(ctx.txs, first, int(ctx.policy.get("TM07.dormant_days")), ctx.customer.onboarded_on if ctx.customer else None)
    gap = dorm["longest_gap"]
    since = [t for t in ctx.txs if gap and t.booked_date >= gap["reactivation_date"] and t.booked_date <= ctx.bundle.case.review_window.end]
    moved = M.total(since)
    af.recomputed = {"dormancy": dorm, "moved_since_reactivation_gbp": moved,
                     "since_reactivation_ids": [t.transaction_id for t in since],
                     "limit_gbp": ctx.policy.get("TM07.min_moved_gbp"), "limit_days": ctx.policy.get("TM07.dormant_days")}
    af.discrepancies = [d for d in (
        discrepancy("dormant_days", a.measured.get("dormant_days"), Decimal(gap["gap_days"]) if gap else None, Decimal(1),
                    "days between the last transaction before the gap and the first after it (the alert may count differently)"),
        discrepancy("moved_gbp", a.measured.get("moved_gbp"), M.total(alerted), Decimal("0.01"), "sum of the alerted transactions"),
    ) if d]
    reverif = [d for d in ctx.doc_facts if d["type"] == "reverification_record" or any(s["signal"] == "identity_reverified" for s in d["signals"])]
    claims = [{"doc_id": d["doc_id"], "quote": s["quote"]} for d in reverif for s in d["signals"] if s["signal"] == "relocation_or_absence"]
    af.observations = {"reactivation_transactions": [tx_brief(t) for t in alerted],
                       "reverification_documents": [{"doc_id": d["doc_id"], "dated": d["dated"]} for d in reverif],
                       "customer_explanations_for_absence": claims,
                       "reactivation_counterparties_own_name": [t.transaction_id for t in alerted if is_own_name(ctx, t)],
                       "reactivation_countries": sorted({t.counterparty_country for t in alerted if t.counterparty_country})}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="dormant_reactivation", rule_id="TM07", observed_value=gap["gap_days"] if gap else None,
        threshold=ctx.policy.get("TM07.dormant_days"), result=(dorm["status"] == "confirmed_by_history") if dorm["status"] != "insufficient_history" else None,
        calculation=f"longest gap before {first}: {gap}", transaction_ids=[x for x in ([gap["last_activity_id"], gap["reactivation_id"]] if gap else [])],
        alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED if dorm["status"] == "confirmed_by_history" else EvidenceStatus.UNKNOWN,
        caveats=[] if dorm["status"] == "confirmed_by_history" else [f"dormancy status: {dorm['status']}"]))
    ids = [t.transaction_id for t in alerted]
    af.candidate_explanations = explanations_for(ctx, ids) + [
        {"doc_id": d["doc_id"], "doc_type": d["type"], "reliability": d["reliability"], "status": EvidenceStatus.VERIFIED,
         "summary": f"{d['doc_id']}: identity re-verified ({d['dated']})"} for d in reverif]
    af.coverage = coverage(ctx, ids)
    af.unresolved_requirements.append({"requirement": "identity of the returning customer re-verified",
                                       "status": EvidenceStatus.VERIFIED if reverif else EvidenceStatus.MISSING,
                                       "detail": ", ".join(d["doc_id"] for d in reverif) or "no re-verification record on file"})
    unsup = [r["transaction_id"] for r in af.coverage["per_transaction"] if r["status"] in (EvidenceStatus.MISSING,) and not is_own_name(ctx, ctx.tx_map[r["transaction_id"]])]
    if unsup:
        af.unresolved_requirements.append({"requirement": "source/purpose of the reactivation transactions", "status": "unresolved",
                                           "transaction_ids": unsup, "detail": f"{len(unsup)} transaction(s) without a supporting document"})


def _relationship_candidates(ctx: Ctx, t: Transaction) -> list[dict]:
    """Documented or name-based links between a third party and the customer (candidates only)."""
    out = []
    cp = t.counterparty_name or ""
    cust = ctx.customer_name or ""
    shared = name_tokens(cp) & name_tokens(cust)
    if shared and not names_equal(cp, cust):
        out.append({"type": "shared_name_words", "detail": f"'{cp}' and '{cust}' share {sorted(shared)}",
                    "status": EvidenceStatus.UNKNOWN, "doc_ids": [], "heuristic": True,
                    "caveat": "a shared surname or initials suggests a family or joint account but proves nothing"})
    for d in ctx.doc_facts:
        reg = d.get("registry") or {}
        members = [m.get("company") for m in reg.get("group_members", [])]
        if members and any(names_equal(m, cp) for m in members) and any(names_equal(m, cust) for m in members):
            owners = sorted({m.get("owner") for m in reg["group_members"] if m.get("owner")})
            out.append({"type": "group_company", "detail": f"{d['doc_id']} lists both companies under {owners}",
                        "status": EvidenceStatus.PARTIALLY_SUPPORTED if d["reliability"] == "customer_prepared_record" else EvidenceStatus.VERIFIED,
                        "doc_ids": [d["doc_id"]], "reliability": d["reliability"]})
        named = [x for x in d["parties"] if names_equal(x["name"], cp)]
        if named and d["category"] in ("source_of_funds_event", "business_records", "payment_support", "corporate_registry"):
            out.append({"type": f"named_in_{d['type']}", "detail": f"{cp} is the {named[0]['role']} in {d['doc_id']} ({d['title']})",
                        "status": EvidenceStatus.PARTIALLY_SUPPORTED, "doc_ids": [d["doc_id"]], "reliability": d["reliability"]})
        if d["reliability"] == "customer_statement" and re.search(re.escape(cp), _doc_text(ctx, d["doc_id"]) or "", re.IGNORECASE):
            out.append({"type": "customer_explanation", "detail": f"{d['doc_id']} mentions {cp}",
                        "status": EvidenceStatus.CLAIMED_ONLY, "doc_ids": [d["doc_id"]], "reliability": d["reliability"]})
    return out


def tm08(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    thr = ctx.policy.get("TM08.min_gbp")
    flows = []
    for t in alerted:
        flows.append({**tx_brief(t), "name_differs_from_customer": not is_own_name(ctx, t),
                      "name_overlap_with_customer": round(token_overlap(t.counterparty_name, ctx.customer_name), 2),
                      "relationship_candidates": _relationship_candidates(ctx, t),
                      "document_support": support_for(ctx, t.transaction_id)})
    af.recomputed = {"flows": len(alerted), "total_gbp": M.total(alerted), "limit_gbp": thr,
                     "over_limit_ids": [t.transaction_id for t in alerted if t.amount_gbp >= thr]}
    af.discrepancies = [d for d in (
        discrepancy("flows", a.measured.get("flows"), Decimal(len(alerted)), Decimal(0), "alerted transactions"),
        discrepancy("total_gbp", a.measured.get("total_gbp"), M.total(alerted), Decimal("0.01"), "sum of the alerted transactions"),
    ) if d]
    later = []
    for t in alerted:
        if t.direction == "in":
            nxt = [x for x in ctx.txs if x.direction == "out" and x.channel != "fund_subscription" and not x.is_card
                   and t.booked_at <= x.booked_at <= t.booked_at + timedelta(days=10)]
            later += [x.transaction_id for x in nxt]
    af.observations = {"flows": flows, "outflows_within_10_days_of_third_party_inflow": sorted(set(later))}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="third_party_flow", rule_id="TM08", observed_value=M.total(alerted), threshold=thr,
        result=any(f["name_differs_from_customer"] for f in flows),
        calculation="counterparty name compared with the customer name (legal suffixes ignored)",
        transaction_ids=[t.transaction_id for t in alerted], alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED))
    ids = [t.transaction_id for t in alerted]
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    for f in flows:
        documented = [c for c in f["relationship_candidates"] if c["status"] in (EvidenceStatus.VERIFIED, EvidenceStatus.PARTIALLY_SUPPORTED)]
        st = f["document_support"]["status"]
        if st != EvidenceStatus.VERIFIED and not documented:
            af.unresolved_requirements.append({"requirement": "documented relationship explaining the third-party payer/payee",
                                               "status": EvidenceStatus.CLAIMED_ONLY if any(c["status"] == EvidenceStatus.CLAIMED_ONLY for c in f["relationship_candidates"]) else EvidenceStatus.MISSING,
                                               "transaction_ids": [f["transaction_id"]],
                                               "detail": f"{f['counterparty_name']} {f['amount_gbp']}"})


def _fund_code(t: Transaction) -> str | None:
    m = re.search(r"FUND (?:BUY|SELL)\s+(\S+)", t.reference or "")
    return m.group(1) if m else None


def tm10(ctx: Ctx, a: Alert, af: AlertFacts) -> None:
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    sells = [t for t in alerted if t.channel == "fund_redemption"] or [t for t in ctx.txs if t.channel == "fund_redemption" and t.transaction_id in a.transaction_ids]
    buys_alerted = [t for t in alerted if t.channel == "fund_subscription"]
    max_days = int(ctx.policy.get("TM10.max_held_days"))
    pairs = []
    for s in sells:
        code = _fund_code(s)
        cands = [b for b in ctx.txs if b.channel == "fund_subscription" and b.booked_at <= s.booked_at
                 and (s.booked_date - b.booked_date).days <= max_days and (code is None or _fund_code(b) == code)]
        b = max(cands, key=lambda x: (x.booked_at, x.transaction_id)) if cands else None
        after = [x for x in ctx.txs if x.direction == "out" and x.channel not in ("fund_subscription",) and s.booked_at <= x.booked_at <= s.booked_at + timedelta(days=10)]
        pairs.append({"redemption": tx_brief(s), "fund_code": code,
                      "purchase": tx_brief(b) if b else None, "held_days": (s.booked_date - b.booked_date).days if b else None,
                      "gain_loss_gbp": (s.amount_gbp - b.amount_gbp) if b else None,
                      "withdrawals_within_10_days": [{**tx_brief(x), "own_name": is_own_name(ctx, x)} for x in after]})
    # how the purchase was funded
    funding = []
    for pr in pairs:
        if pr["purchase"]:
            bt = pr["purchase"]["booked_at"]
            dep = [x for x in ctx.txs if x.direction == "in" and x.channel != "fund_redemption" and bt - timedelta(days=10) <= x.booked_at <= bt]
            funding.append({"purchase_id": pr["purchase"]["transaction_id"],
                            "deposits_in_10_days_before": [{**tx_brief(x), "own_name": is_own_name(ctx, x)} for x in dep]})
    held = pairs[0]["held_days"] if pairs else None
    af.recomputed = {"pairs": pairs, "limit_days": max_days, "purchase_funding": funding}
    af.discrepancies = [d for d in (
        discrepancy("held_days", a.measured.get("held_days"), Decimal(held) if held is not None else None, Decimal(0), "redemption date minus matched purchase date"),
        discrepancy("bought_gbp", a.measured.get("bought_gbp"), pairs[0]["purchase"]["amount_gbp"] if pairs and pairs[0]["purchase"] else None, Decimal("0.01"), "matched purchase"),
        discrepancy("redeemed_gbp", a.measured.get("redeemed_gbp"), pairs[0]["redemption"]["amount_gbp"] if pairs else None, Decimal("0.01"), "redemption"),
    ) if d]
    third_wd = [w for pr in pairs for w in pr["withdrawals_within_10_days"] if not w["own_name"]]
    af.observations = {"fund": a.measured.get("fund"), "alerted_purchases": [t.transaction_id for t in buys_alerted],
                       "withdrawals_to_third_parties_after_redemption": [w["transaction_id"] for w in third_wd]}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="early_redemption", rule_id="TM10", observed_value=held, threshold=max_days,
        result=None if held is None else held < max_days, calculation="days between fund purchase and redemption (same fund code)",
        transaction_ids=[x for pr in pairs for x in ([pr["redemption"]["transaction_id"]] + ([pr["purchase"]["transaction_id"]] if pr["purchase"] else []))],
        alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED if held is not None else EvidenceStatus.UNKNOWN))
    if third_wd:
        ctx.indicator(indicator_type="redemption_paid_to_third_party", rule_id="TM10", observed_value=M.total([ctx.tx_map[w["transaction_id"]] for w in third_wd]),
                      threshold=None, result=True, calculation="withdrawals within 10 days of the redemption to a name other than the customer's",
                      transaction_ids=[w["transaction_id"] for w in third_wd], alert_ids=[a.alert_id], verification_status=EvidenceStatus.VERIFIED)
    ids = [x for pr in pairs for x in ([pr["redemption"]["transaction_id"]] + ([pr["purchase"]["transaction_id"]] if pr["purchase"] else []))]
    reasons = [d for d in ctx.doc_facts if d["type"] in ("customer_email", "redundancy_letter", "customer_letter", "support_chat")]
    af.candidate_explanations = explanations_for(ctx, ids) + [
        {"doc_id": d["doc_id"], "doc_type": d["type"], "reliability": d["reliability"],
         "status": EvidenceStatus.CLAIMED_ONLY if d["reliability"] == "customer_statement" else EvidenceStatus.PARTIALLY_SUPPORTED,
         "summary": f"{d['doc_id']} {d['title']}"} for d in reasons]
    af.coverage = coverage(ctx, ids)
    af.unresolved_requirements.append({"requirement": "reason for the early redemption",
                                       "status": (EvidenceStatus.PARTIALLY_SUPPORTED if any(d["reliability"] == "third_party_document" for d in reasons)
                                                  else EvidenceStatus.CLAIMED_ONLY if reasons else EvidenceStatus.MISSING),
                                       "detail": ", ".join(d["doc_id"] for d in reasons) or "nothing on file"})


def tm22(ctx: Ctx, a: Alert, af: AlertFacts, list_out: list[ListMatchCandidate]) -> None:
    eid = a.measured.get("list_entry")
    entry = ctx.list_entries.get(eid) if isinstance(eid, str) else None
    alerted = [ctx.tx_map[t] for t in a.transaction_ids if t in ctx.tx_map]
    if entry is None:
        af.recomputed = {"list_entry_found": False}
        af.unresolved_requirements.append({"requirement": "internal-list entry cited by the alert", "status": EvidenceStatus.MISSING,
                                           "detail": f"{eid} is not in internal_list.jsonl"})
        return
    cand = assess_entry(entry, alert_ids=[a.alert_id], transactions=alerted, alert_matched_on=a.measured.get("matched_on"),
                        doc_facts=ctx.doc_facts, customer_name=ctx.customer_name)
    list_out.append(cand)
    af.recomputed = {"list_entry_found": True, "candidate_confidence": cand.candidate_confidence,
                     "matched_fields": cand.matched_fields, "conflicting_fields": cand.conflicting_fields,
                     "unknown_fields": cand.unknown_fields, "control_relationship": cand.control_relationship}
    af.observations = {"list_entry": dict(entry.raw), "matched_party_in_alert": a.measured.get("matched_party"),
                       "matched_on_in_alert": a.measured.get("matched_on"), "comparisons": cand.comparisons,
                       "payments": [tx_brief(t) for t in alerted]}
    af.risk_indicator_ids.append(ctx.indicator(
        indicator_type="internal_list_candidate", rule_id="TM22", observed_value=cand.candidate_confidence, threshold=None,
        result=None if cand.candidate_confidence in ("possible", "none") else cand.candidate_confidence in ("identity_corroborated", "probable"),
        calculation=cand.explanation, transaction_ids=[t.transaction_id for t in alerted], supporting_ids=[entry.entry_id] + cand.supporting_ids,
        alert_ids=[a.alert_id], caveats=["a single matching detail does not prove the same party"],
        verification_status=EvidenceStatus.VERIFIED if cand.candidate_confidence in ("identity_corroborated", "identifier_conflict", "address_premises_differ") else EvidenceStatus.UNKNOWN))
    ids = [t.transaction_id for t in alerted]
    af.candidate_explanations = explanations_for(ctx, ids)
    af.coverage = coverage(ctx, ids)
    resolved = {"identifier_conflict": "evidence indicates a different party", "address_premises_differ": "evidence indicates a different party",
                "identity_corroborated": "evidence indicates the listed party", "probable": "evidence leans to the listed party"}
    ctrl = cand.control_relationship.get("status")
    status = resolved.get(cand.candidate_confidence, "unresolved")
    if ctrl == "in_role_on_payment_date":
        status = "listed person held a role at the counterparty on the payment date"
    af.unresolved_requirements.append({"requirement": "whether the counterparty is, or is controlled by, the listed party",
                                       "status": status, "detail": cand.explanation})


ANALYSERS: dict[str, Callable] = {"TM01": tm01, "TM02": tm02, "TM03": tm03, "TM04": tm04, "TM05": tm05,
                                  "TM06": tm06, "TM07": tm07, "TM08": tm08, "TM10": tm10}


def held_payment_requirements(ctx: Ctx) -> list[dict]:
    """Payments and FX: a held payment of the policy amount or more to anyone other than the client needs an invoice or contract."""
    held = ctx.bundle.held_payment
    thr = ctx.policy.get("invoice_required.min_gbp")
    if held is None or thr is None:
        return []
    own = is_own_name(ctx, held)
    req = {"requirement": f"invoice or contract on file for a payment of {thr} or more to a party other than the client",
           "transaction_ids": [held.transaction_id], "applies": held.amount_gbp >= thr and not own,
           "held_amount_gbp": held.amount_gbp, "payee": held.counterparty_name, "payee_is_client_name": own}
    if not req["applies"]:
        req.update(status="not_applicable", detail="below the threshold" if held.amount_gbp < thr else "payee carries the client's own name")
        if own:
            oe = ownership_evidence(ctx, held)
            req["own_account_evidence"] = oe
        return [req]
    ms = [m for m in ctx.matches_by_tx.get(held.transaction_id, []) if ctx.doc_by_id[m.doc_id]["category"] == "payment_support"]
    if any(m.strength == "strong" and m.verification_status == EvidenceStatus.VERIFIED for m in ms):
        st = EvidenceStatus.VERIFIED
    elif any(m.strength == "conflicting" for m in ms) and not any(m.strength in SUPPORTING for m in ms):
        st = EvidenceStatus.CONTRADICTED
    elif ms:
        st = EvidenceStatus.PARTIALLY_SUPPORTED
    else:
        st = EvidenceStatus.MISSING
    req.update(status=st, match_ids=[m.match_id for m in ms], doc_ids=sorted({m.doc_id for m in ms}),
               detail="; ".join(m.explanation for m in ms) or "no invoice, contract or payment file on file matches the held payment")
    return [req]
