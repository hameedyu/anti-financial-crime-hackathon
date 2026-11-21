"""Person 2 - fact extraction.

Public API::

    facts = FactExtractor().extract(bundle)      # or extract_case_facts(bundle)
    payload = serialize_case_facts(facts)        # JSON-safe dict
"""
from __future__ import annotations

from data_processing.serialize import canonical_json, to_jsonable
from .extractor import FactExtractor, extract_case_facts
from .models import SCHEMA_VERSION, CaseFacts, EvidenceStatus, Reliability
from .provenance import ProvenanceIndex, validate_evidence_ids


def serialize_case_facts(facts: CaseFacts) -> dict:
    return to_jsonable(facts)


__all__ = ["FactExtractor", "extract_case_facts", "serialize_case_facts", "canonical_json", "CaseFacts",
           "EvidenceStatus", "Reliability", "SCHEMA_VERSION", "ProvenanceIndex", "validate_evidence_ids"]
