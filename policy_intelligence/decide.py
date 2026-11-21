from reason_codes import CLOSE_CODES, RFI_CODES, ESCALATE_CODES

def decide_case(case: dict, alerts: list, customer: dict, evidence_ids: list = None, facts: dict = None) -> dict:
    """
    Decision function.
    If facts (from Person 2) are provided, use the richer logic.
    Otherwise fall back to simple rule-based logic.
    """
    alert_ids = case.get("alert_ids", [])
    rules_fired = [a.get("rule_id") for a in alerts if a.get("alert_id") in alert_ids]
    
    # Default conservative
    verdict = "RFI"
    reason_code = "RFI_SOURCE_OF_FUNDS"
    reason = "Activity is not fully explained by current evidence. Requesting source of funds / purpose."
    evidence = list(alert_ids)

    # --- Rich logic when facts are available ---
    if facts:
        source_status = facts.get("source_of_funds", {}).get("status", "unknown")
        supporting = facts.get("supporting_explanations", [])
        contradicting = facts.get("contradicting_evidence", [])
        list_matches = facts.get("internal_list_matches", [])

        # Escalate on list matches or mule-like patterns
        if "TM22" in rules_fired or list_matches:
            verdict = "ESCALATE"
            reason_code = "ESC_LISTED_PARTY"
            reason = "Internal list match or control relationship identified. Escalate for MLRO review."
        elif "TM02" in rules_fired or "TM04" in rules_fired:
            verdict = "ESCALATE"
            reason_code = "ESC_MULE_ACTIVITY"
            reason = "Rapid movement or many-to-one pattern consistent with possible mule activity."
        # Close when strong supporting evidence and no contradictions
        elif source_status in ("verified", "partially_supported") and supporting and not contradicting:
            if "TM01" in rules_fired or "TM06" in rules_fired or "TM05" in rules_fired:
                verdict = "CLOSE"
                reason_code = "WITHIN_PROFILE"
                reason = "Alerted activity is supported by third-party documentation and consistent with explained purpose."
            else:
                verdict = "RFI"
                reason_code = "RFI_UPDATED_PROFILE"
                reason = "Partial support exists; requesting updated profile or additional documents."
        else:
            # Keep existing rule-based RFI logic
            if "TM01" in rules_fired:
                reason_code = "RFI_UPDATED_PROFILE"
                reason = "Activity exceeds declared profile. Request updated expected activity."
            elif "TM06" in rules_fired:
                reason_code = "RFI_UPDATED_PROFILE"
                reason = "Activity involving a higher-risk country not declared by the customer."
            elif "TM05" in rules_fired or "TM03" in rules_fired:
                reason_code = "RFI_SOURCE_OF_FUNDS"
                reason = "Large or repeated cash / near-threshold activity. Request source of funds."
            elif "TM07" in rules_fired:
                reason_code = "RFI_PURPOSE_DOCUMENTS"
                reason = "Dormant account reactivated. Request purpose of the new activity."
            elif "TM08" in rules_fired:
                reason_code = "RFI_COUNTERPARTY_DETAILS"
                reason = "Third-party activity detected. Request explanation."

        # Better evidence from facts
        evidence = []
        for s in supporting:
            if s.get("doc_id"):
                evidence.append(s["doc_id"])
            evidence.extend(s.get("transaction_ids", []))
        evidence.extend(alert_ids)

    # --- Fallback simple logic (when no facts) ---
    else:
        if "TM22" in rules_fired:
            verdict = "ESCALATE"
            reason_code = "ESC_LISTED_PARTY"
            reason = "Counterparty appears on the internal list. Escalate for MLRO review."
        elif "TM02" in rules_fired or "TM04" in rules_fired:
            verdict = "ESCALATE"
            reason_code = "ESC_MULE_ACTIVITY"
            reason = "Rapid movement of funds or many-to-one pattern consistent with possible mule activity."
        elif "TM01" in rules_fired:
            reason_code = "RFI_UPDATED_PROFILE"
            reason = "Activity significantly exceeds the customer's declared profile. Request updated expected activity."
        elif "TM05" in rules_fired or "TM03" in rules_fired:
            reason_code = "RFI_SOURCE_OF_FUNDS"
            reason = "Large or repeated cash / near-threshold activity. Request source of funds documentation."
        elif "TM06" in rules_fired:
            reason_code = "RFI_UPDATED_PROFILE"
            reason = "Activity involving a higher-risk country not declared by the customer."
        elif "TM07" in rules_fired:
            reason_code = "RFI_PURPOSE_DOCUMENTS"
            reason = "Dormant account reactivated. Request purpose of the new activity."
        elif "TM08" in rules_fired:
            reason_code = "RFI_COUNTERPARTY_DETAILS"
            reason = "Third-party funding or payout detected. Request explanation and supporting documents."

        evidence = list(alert_ids)
        for a in alerts:
            if a.get("alert_id") in alert_ids:
                evidence.extend(a.get("transaction_ids", []))

    # Deduplicate
    seen = set()
    unique_evidence = []
    for e in evidence:
        if e and e not in seen:
            seen.add(e)
            unique_evidence.append(e)

    return {
        "case_id": case.get("case_id"),
        "alert_ids": alert_ids,
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "evidence": unique_evidence[:10]
    }