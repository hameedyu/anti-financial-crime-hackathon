"""Person 1 - case ingestion.

Public API::

    repo = CaseRepository("/abs/path/institutions/bank")   # or load_institution(path)
    bundle = repo.get_case_bundle("BK-CS...")
    for bundle in repo.iter_case_bundles(): ...
    issues = validate_case_bundle(bundle)
    payload = serialize_case_bundle(bundle)
"""
from .loaders import (
    IngestionError, load_alerts, load_cases, load_customers, load_evidence_document,
    load_evidence_index, load_internal_list, load_previous_reviews, load_transactions,
)
from .models import SCHEMA_VERSION, CaseBundle
from .policy import load_policy
from .repository import CaseNotFound, CaseRepository, load_institution
from .serialize import canonical_json, serialize_case_bundle, to_jsonable
from .validators import validate_case_bundle

__all__ = [
    "SCHEMA_VERSION", "CaseBundle", "CaseRepository", "CaseNotFound", "IngestionError",
    "load_institution", "load_cases", "load_alerts", "load_customers", "load_transactions",
    "load_evidence_index", "load_evidence_document", "load_previous_reviews", "load_internal_list",
    "load_policy", "validate_case_bundle", "serialize_case_bundle", "canonical_json", "to_jsonable",
]
