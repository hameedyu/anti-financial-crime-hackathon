from reason_codes import CLOSE_CODES, RFI_CODES, ESCALATE_CODES
from decision_tables import RULE_TO_REASONCODES

def decide_case(case: dict, alerts: list, customer: dict, evidence_ids: list) -> dict:
    alert_ids = case.get("alert_ids", [])
    rules_fired = [a.get("rule_id") for a in alerts if a.get("alert_id") in alert_ids]

    # Default – conservative
    verdict = "RFI"
    reason_code = "RFI_SOURCE_OF_FUNDS"
    reason = "Activity is not fully explained by current evidence. Requesting source of funds / purpose."

    if "TM22" in rules_fired:
        verdict = "ESCALATE"
        reason_code = "ESC_LISTED_PARTY"
        reason = "Counterparty appears on the internal list. Escalate for MLRO review."

    elif "TM02" in rules_fired or "TM04" in rules_fired:
        verdict = "ESCALATE"
        reason_code = "ESC_MULE_ACTIVITY"
        reason = "Rapid movement of funds or many-to-one pattern consistent with possible mule activity."

    elif "TM01" in rules_fired:
        verdict = "RFI"
        reason_code = "RFI_UPDATED_PROFILE"
        reason = "Activity significantly exceeds the customer's declared profile. Request updated expected activity."

    elif "TM05" in rules_fired or "TM03" in rules_fired:
        verdict = "RFI"
        reason_code = "RFI_SOURCE_OF_FUNDS"
        reason = "Large or repeated cash / near-threshold activity. Request source of funds documentation."

    elif "TM06" in rules_fired:
        verdict = "RFI"
        reason_code = "RFI_UPDATED_PROFILE"
        reason = "Activity involving a higher-risk country not declared by the customer."

    elif "TM07" in rules_fired:
        verdict = "RFI"
        reason_code = "RFI_PURPOSE_DOCUMENTS"
        reason = "Dormant account reactivated. Request purpose of the new activity."

    elif "TM08" in rules_fired:
        verdict = "RFI"
        reason_code = "RFI_COUNTERPARTY_DETAILS"
        reason = "Third-party funding or payout detected. Request explanation and supporting documents."

    # Build real evidence list
    evidence = list(alert_ids)
    for a in alerts:
        if a.get("alert_id") in alert_ids:
            evidence.extend(a.get("transaction_ids", []))

    # Deduplicate while preserving order
    seen = set()
    unique_evidence = []
    for e in evidence:
        if e and e not in seen:
            seen.add(e)
            unique_evidence.append(e)

    result = {
        "case_id": case.get("case_id"),
        "alert_ids": alert_ids,
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "evidence": unique_evidence[:10]
    }

    return result