# decision_tables.py

BANK_POLICY = {
    "OWN_ACCOUNT": {
        "under_5x_profile": "CLOSE",
        "over_5x_profile": "RFI",
    },
    "INTERNAL_LIST": {
        "different_party": "CLOSE",
        "unclear": "RFI",
        "same_party": "ESCALATE",
    },
}

# Maps each rule to the allowed reason codes by verdict
RULE_TO_REASONCODES = {
    "TM01": {
        "close": ["WITHIN_PROFILE", "DOCUMENTED_EVENT", "OWN_ACCOUNT"],
        "rfi": ["RFI_SOURCE_OF_FUNDS", "RFI_UPDATED_PROFILE"],
        "escalate": ["ESC_UNEXPLAINED_FUNDS", "ESC_MULE_ACTIVITY"],
    },
    "TM02": {
        "close": ["DOCUMENTED_EVENT", "OWN_ACCOUNT", "REFUND_OF_EARLIER_PAYMENT"],
        "rfi": ["RFI_SOURCE_OF_FUNDS", "RFI_PURPOSE_DOCUMENTS"],
        "escalate": ["ESC_PASS_THROUGH", "ESC_MULE_ACTIVITY", "ESC_UNEXPLAINED_FUNDS"],
    },
    "TM03": {
        "close": ["DOCUMENTED_EVENT", "WITHIN_PROFILE"],
        "rfi": ["RFI_SOURCE_OF_FUNDS", "RFI_BUSINESS_RECORDS"],
        "escalate": ["ESC_STRUCTURING", "ESC_UNEXPLAINED_FUNDS"],
    },
    "TM04": {
        "close": ["DOCUMENTED_EVENT", "COLLECTION_DOCUMENTED"],
        "rfi": ["RFI_COUNTERPARTY_DETAILS", "RFI_PURPOSE_DOCUMENTS"],
        "escalate": ["ESC_MULE_ACTIVITY", "ESC_PASS_THROUGH"],
    },
    "TM05": {
        "close": ["DOCUMENTED_EVENT", "OWN_ACCOUNT"],
        "rfi": ["RFI_SOURCE_OF_FUNDS", "RFI_PURPOSE_DOCUMENTS"],
        "escalate": ["ESC_UNEXPLAINED_FUNDS", "ESC_STRUCTURING"],
    },
    "TM06": {
        "close": ["WITHIN_PROFILE", "DOCUMENTED_EVENT"],
        "rfi": ["RFI_UPDATED_PROFILE", "RFI_PURPOSE_DOCUMENTS"],
        "escalate": ["ESC_HIGH_RISK_CORRIDOR", "ESC_UNEXPLAINED_FUNDS"],
    },
    "TM07": {
        "close": ["RETURNING_CUSTOMER", "DOCUMENTED_EVENT"],
        "rfi": ["RFI_PURPOSE_DOCUMENTS", "RFI_UPDATED_PROFILE"],
        "escalate": ["ESC_ACCOUNT_MISUSE", "ESC_UNEXPLAINED_FUNDS"],
    },
    "TM08": {
        "close": ["THIRD_PARTY_DOCUMENTED", "OWN_ACCOUNT"],
        "rfi": ["RFI_COUNTERPARTY_DETAILS", "RFI_SOURCE_OF_FUNDS"],
        "escalate": ["ESC_THIRD_PARTY_FUNDING", "ESC_MULE_ACTIVITY"],
    },
    "TM22": {
        "close": ["LIST_MATCH_DIFFERENT_PARTY"],
        "rfi": ["RFI_COUNTERPARTY_DETAILS"],
        "escalate": ["ESC_LISTED_PARTY"],
    },
}