"""Applicability of earlier alert reviews to the current case.

POLICY.md (all four institutions): "An earlier review covers only what it
describes. It does not explain new or larger activity." So a review is at
most *context* for the current alerts. This module states, per review, what
it relates to and why it does or does not cover the current activity.
"""
from __future__ import annotations

import re

from data_processing.models import CaseBundle, Transaction
from data_processing.textutil import find_money, names_equal
from .models import ReviewApplicability

# words in a review note that refer to a kind of document, and the document
# types or title words that would satisfy them
_DOC_WORDS = {
    "payslip": ("payslip",), "booking": ("booking",), "invoice": ("invoice",), "contract": ("contract",),
    "self-assessment": ("self-assessment", "tax"), "statement": ("statement",), "receipt": ("receipt",),
    "letter": ("letter",),
}
_NAME = re.compile(r"\b([A-Z][a-z]+(?: (?:[A-Z][a-z]+|and|&|SAL|Ltd|LLP|plc|FZE|Srl|Co))+)\b")


def assess_reviews(bundle: CaseBundle, txs: list[Transaction], doc_facts: list[dict]) -> list[ReviewApplicability]:
    out = []
    window = bundle.case.review_window
    rules_now: dict[str, list[str]] = {}
    for a in bundle.alerts:
        rules_now.setdefault(a.rule_id, []).append(a.alert_id)
    alerted_ids = set(bundle.alert_transaction_ids())
    alerted = [t for t in txs if t.transaction_id in alerted_ids]
    earliest_alerted = min((t.booked_date for t in alerted), default=window.start)
    for r in bundle.previous_reviews:
        reasons = []
        same = r.rule_id in rules_now
        mentions = []
        for nm in _NAME.findall(r.note):
            for t in alerted:
                if t.counterparty_name and names_equal(nm, t.counterparty_name):
                    mentions.append(f"{nm} (counterparty of {t.transaction_id})")
        amounts = [a for a, *_ in find_money(r.note)]
        note_l = r.note.lower()
        wanted = [k for k in _DOC_WORDS if k in note_l]
        on_file = sorted({d["doc_id"] for d in doc_facts for k in wanted
                          if any(w in (d["type"] + " " + (d["title"] or "")).lower() for w in _DOC_WORDS[k])})
        if r.reviewed_on < earliest_alerted:
            covers = "no"
            reasons.append(f"reviewed on {r.reviewed_on}, before the alerted activity began ({earliest_alerted})")
        else:
            covers = "not_established"
            reasons.append(f"reviewed on {r.reviewed_on}, on or after the alerted activity began ({earliest_alerted})")
        reasons.append("same rule as a current alert" if same else "different rule from the current alerts")
        if mentions:
            reasons.append("the note names a counterparty of the current alerted transactions")
        else:
            reasons.append("the note does not name any counterparty of the current alerted transactions")
        if amounts:
            cur_max = max((t.amount_gbp for t in alerted), default=None)
            if cur_max is not None and cur_max > max(amounts):
                reasons.append(f"current alerted amounts (up to {cur_max}) exceed the amounts in the note")
        if wanted:
            reasons.append(f"note refers to {', '.join(wanted)} on file; matching documents now on file: {', '.join(on_file) or 'none'}")
        reasons.append("policy: an earlier review covers only what it describes and does not explain new or larger activity")
        assessment = ("context only: same rule and counterparty as current activity, but it predates it" if same and mentions
                      else "context only" if same else "not relevant to the current alerts")
        out.append(ReviewApplicability(
            review_id=r.review_id, rule_id=r.rule_id, reviewed_on=r.reviewed_on.isoformat(), verdict=r.verdict, note=r.note,
            days_before_window=(window.start - r.reviewed_on).days, same_rule_as_current_alerts=same,
            current_alert_ids_same_rule=rules_now.get(r.rule_id, []), note_mentions=sorted(set(mentions)),
            referenced_documents_on_file=on_file, covers_current_activity=covers, assessment=assessment, reasons=reasons))
    return out
