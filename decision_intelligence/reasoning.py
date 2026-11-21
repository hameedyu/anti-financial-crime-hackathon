#!/usr/bin/env python3
"""Schema-flexible reasoning engine for the TM hackathon.

Person 4 owns this file. It does not assume a fixed facts schema.
Whatever JSON Person 2 produces is sent to the model, together with
whatever policy text Person 3 retrieves.

Constraints this file respects:
- Python standard library only
- LLM calls only via OpenRouter
- temperature 0, fixed seed, on-disk cache so repeated runs match
- a bad model response becomes ESCALATE, never a guessed CLOSE

Usage from the runner:

    from reasoning import decide
    row = decide(case_facts, policy_excerpts)

Or from the shell, for a single case:

    python reasoning.py facts.json policy.txt
"""

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Non-reasoning Grok. About $1.25 / $2.50 per million tokens.
# 200 cases with a few thousand tokens each stays under the $4 budget.
# Override with GROK_MODEL if OpenRouter renames it.
DEFAULT_MODEL = "x-ai/grok-4.20"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")

REASON_CODES = {
    "CLOSE": [
        "COLLECTION_DOCUMENTED",
        "BUSINESS_RECORDS_RECONCILE",
        "DOCUMENTED_EVENT",
        "INVOICE_MATCHES",
        "OWN_ACCOUNT",
        "GROUP_COMPANY",
        "THIRD_PARTY_DOCUMENTED",
        "REFUND_OF_EARLIER_PAYMENT",
        "RETURNING_CUSTOMER",
        "LIST_MATCH_DIFFERENT_PARTY",
        "WITHIN_PROFILE",
    ],
    "RFI": [
        "RFI_PURPOSE_DOCUMENTS",
        "RFI_BUSINESS_RECORDS",
        "RFI_SOURCE_OF_FUNDS",
        "RFI_COUNTERPARTY_DETAILS",
        "RFI_UPDATED_PROFILE",
    ],
    "ESCALATE": [
        "ESC_MULE_ACTIVITY",
        "ESC_STRUCTURING",
        "ESC_UNEXPLAINED_FUNDS",
        "ESC_PASS_THROUGH",
        "ESC_INVOICE_MISMATCH",
        "ESC_HIGH_RISK_CORRIDOR",
        "ESC_ACCOUNT_MISUSE",
        "ESC_THIRD_PARTY_FUNDING",
        "ESC_EARLY_REDEMPTION",
        "ESC_LISTED_PARTY",
    ],
}

SYSTEM_PROMPT = """You are the decision step of a UK transaction-monitoring system.
You receive case facts as JSON. The JSON schema is not fixed. Read whatever keys are present: alerts, customer profile, transactions, documents, list matches, previous reviews, scores, booleans, free text.

You also receive policy excerpts from the institution's SOP. Apply those excerpts. Where an excerpt is silent, apply the rules below.

Output one verdict for the whole case.

Verdicts
- CLOSE only when the evidence on file explains every alert. If any alert is unexplained, do not CLOSE.
- RFI only when nothing is suspicious yet and one specific document or answer from the customer would settle the case. The reason must say exactly what to ask for.
- ESCALATE when the activity is unexplained or there are signs of crime. Once there is suspicion, ESCALATE. Never RFI after suspicion, because asking the customer can tip them off.

Hard rules
- One verdict covers every alert in the case.
- Own-account transfers explain activity only when a document shows the customer holds the other account. If such a transfer is 5 times the declared income or volume or more, the verdict is RFI with reason code RFI_UPDATED_PROFILE. Below 5 times, CLOSE with OWN_ACCOUNT.
- A list match on one detail (name, address, date of birth, or identifier) does not prove it is the same party. CLOSE with LIST_MATCH_DIFFERENT_PARTY only when evidence shows a different party. If nothing settles it and nothing else is concerning, RFI. If the listed party is the counterparty or controls it, ESCALATE with ESC_LISTED_PARTY.
- An earlier review covers only what it describes. It does not explain new or larger activity.
- At a payments and FX firm, an outgoing payment of £10,000 or more to a non-client needs an invoice or contract on file. If the invoices do not support the payment, ESCALATE with ESC_INVOICE_MISMATCH.
- Round amounts, a shared device or IP, a recipient added the same day, and crypto or gambling counterparties are context only. They do not decide the case by themselves.
- A wrongful CLOSE is the worst mistake. If you are unsure, ESCALATE.

Reason codes, and only these, must match the verdict:
CLOSE: COLLECTION_DOCUMENTED, BUSINESS_RECORDS_RECONCILE, DOCUMENTED_EVENT, INVOICE_MATCHES, OWN_ACCOUNT, GROUP_COMPANY, THIRD_PARTY_DOCUMENTED, REFUND_OF_EARLIER_PAYMENT, RETURNING_CUSTOMER, LIST_MATCH_DIFFERENT_PARTY, WITHIN_PROFILE
RFI: RFI_PURPOSE_DOCUMENTS, RFI_BUSINESS_RECORDS, RFI_SOURCE_OF_FUNDS, RFI_COUNTERPARTY_DETAILS, RFI_UPDATED_PROFILE
ESCALATE: ESC_MULE_ACTIVITY, ESC_STRUCTURING, ESC_UNEXPLAINED_FUNDS, ESC_PASS_THROUGH, ESC_INVOICE_MISMATCH, ESC_HIGH_RISK_CORRIDOR, ESC_ACCOUNT_MISUSE, ESC_THIRD_PARTY_FUNDING, ESC_EARLY_REDEMPTION, ESC_LISTED_PARTY

Reply with one JSON object and nothing else:
{
  "verdict": "CLOSE" or "RFI" or "ESCALATE",
  "reason_code": "one code from the list for that verdict",
  "reason": "two to four sentences naming this case's amounts, names and documents. For an RFI, say what to ask for.",
  "evidence": ["IDs that appear in the facts, such as document, transaction or list-entry IDs"]
}
Do not invent IDs. If no ID is present in the facts, use an empty evidence list.
"""


def decide(case_facts, policy_excerpts=None, case_id=None, alert_ids=None):
    """Return the verdict row for one case.

    case_facts: any JSON-serialisable object. A dict is typical, but a
    list or a raw string is accepted so the caller is not locked to one schema.
    policy_excerpts: a string, or a list of strings, from the policy retriever.
    case_id / alert_ids: optional. Copied onto the result when the facts
    do not already contain them.
    """
    facts_text = _stringify(case_facts, limit=24000)
    policy_text = _stringify(policy_excerpts or [], limit=8000)
    found_case_id, found_alerts = _find_ids(case_facts)
    case_id = case_id or found_case_id or ""
    alert_ids = alert_ids or found_alerts or []

    cache_key = _cache_key(facts_text, policy_text)
    cached = _cache_get(cache_key)
    if cached is not None:
        return _attach_ids(cached, case_id, alert_ids)

    try:
        raw = _call_model(facts_text, policy_text)
        parsed = _parse_json(raw)
        verdict = _validate(parsed)
    except Exception:
        verdict = _fallback(case_id, alert_ids)

    _cache_put(cache_key, verdict)
    return _attach_ids(verdict, case_id, alert_ids)


def _call_model(facts_text, policy_text):
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    model = os.environ.get("GROK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    user = (
        "Policy excerpts:\n"
        + (policy_text or "(none supplied; apply the standing rules)")
        + "\n\nCase facts JSON:\n"
        + facts_text
    )
    body = {
        "model": model,
        "temperature": 0,
        "seed": 7,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
        # Ask the model not to spend the budget on hidden reasoning tokens.
        "reasoning": {"effort": "none"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=data,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("OpenRouter HTTP %s: %s" % (exc.code, detail))

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if not content.strip():
        raise RuntimeError("OpenRouter returned an empty message")
    return content


def _parse_json(text):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def _validate(parsed):
    if not isinstance(parsed, dict):
        raise ValueError("model output is not an object")
    verdict = str(parsed.get("verdict", "")).strip().upper()
    if verdict not in REASON_CODES:
        raise ValueError("bad verdict")
    reason_code = str(parsed.get("reason_code", "")).strip().upper()
    if reason_code not in REASON_CODES[verdict]:
        raise ValueError("reason code does not belong to the verdict")
    reason = str(parsed.get("reason", "")).strip()
    if len(reason) < 40:
        raise ValueError("reason is too short to be specific")
    evidence = parsed.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(item).strip() for item in evidence if str(item).strip()]
    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "reason": reason,
        "evidence": evidence,
    }


def _fallback(case_id, alert_ids):
    # A missing or broken model call must not CLOSE a case.
    return {
        "case_id": case_id or "",
        "alert_ids": list(alert_ids or []),
        "verdict": "ESCALATE",
        "reason_code": "ESC_UNEXPLAINED_FUNDS",
        "reason": (
            "The reasoning step could not produce a reliable decision for this case. "
            "The activity is therefore treated as unexplained and sent to the MLRO "
            "rather than closed."
        ),
        "evidence": [],
    }


def _attach_ids(verdict, case_id, alert_ids):
    out = dict(verdict)
    out["case_id"] = out.get("case_id") or case_id or ""
    if not out.get("alert_ids"):
        out["alert_ids"] = list(alert_ids or [])
    # Stable key order for the output line.
    return {
        "case_id": out["case_id"],
        "alert_ids": out["alert_ids"],
        "verdict": out["verdict"],
        "reason_code": out["reason_code"],
        "reason": out["reason"],
        "evidence": out["evidence"],
    }


def _stringify(value, limit):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n...[truncated]"
    return text


def _find_ids(facts):
    if not isinstance(facts, dict):
        return None, None
    case_id = facts.get("case_id")
    alert_ids = facts.get("alert_ids")
    if not alert_ids and isinstance(facts.get("alerts"), list):
        alert_ids = []
        for alert in facts["alerts"]:
            if isinstance(alert, dict):
                found = alert.get("alert_id") or alert.get("id")
                if found:
                    alert_ids.append(found)
            elif isinstance(alert, str):
                alert_ids.append(alert)
    return case_id, alert_ids


def _cache_key(facts_text, policy_text):
    model = os.environ.get("GROK_MODEL", DEFAULT_MODEL)
    blob = (model + "\n" + SYSTEM_PROMPT + "\n" + policy_text + "\n" + facts_text).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _cache_get(key):
    path = os.path.join(CACHE_DIR, key + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _cache_put(key, verdict):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(verdict, handle, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def main(argv):
    if len(argv) < 2:
        print("usage: python reasoning.py <facts.json> [policy.txt]", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8") as handle:
        facts = json.load(handle)
    excerpts = []
    if len(argv) >= 3:
        with open(argv[2], "r", encoding="utf-8") as handle:
            excerpts = [handle.read()]
    result = decide(facts, excerpts)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
