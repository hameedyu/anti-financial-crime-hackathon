"""Integration surface of Person 1 (ingestion) and Person 2 (facts) for Persons 3 and 4."""
from evidence_intelligence import FactExtractor, extract_case_facts, serialize_case_facts
from data_processing import CaseRepository, serialize_case_bundle, validate_case_bundle
from .integration import (
    PolicyRetrievalInput, ReasoningInput, build_policy_retrieval_input, build_reasoning_input, process_institution,
)

__all__ = ["CaseRepository", "FactExtractor", "extract_case_facts", "serialize_case_bundle", "serialize_case_facts",
           "validate_case_bundle", "process_institution", "build_policy_retrieval_input", "build_reasoning_input",
           "PolicyRetrievalInput", "ReasoningInput"]
