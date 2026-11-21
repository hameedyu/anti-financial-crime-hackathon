# Person 1 (case ingestion) and Person 2 (fact extraction)

These modules turn one institution folder into **case bundles** and, for each bundle, into **case facts**: structured findings that each cite their source records. They never choose a verdict or a reason code. That belongs to Decision Intelligence (Person 4). They also leave the final submission contract (`system/run <institution> <output>`) to the decision stage.

Python 3.11+ standard library only. Deterministic. No LLM calls.

## Architecture

```
data_processing/        Person 1 - case ingestion
    config.py           where the dataset is (AFC_INSTITUTIONS_DIR, or ../hackathon/institutions)
    models.py           frozen dataclasses: Case, Alert, Customer, Transaction, EvidenceDocument, CaseBundle, ...
    loaders.py          strict JSONL loaders (Decimal numbers, UTC datetimes, file:line errors)
    document_reader.py  Markdown -> fields / tables / paragraphs; optional PDF (pypdf if installed), optional CSV
    policy.py           reads limits and the higher-risk country list from POLICY.md, keeping the source text of each value
    repository.py       CaseRepository: load once, index, assemble bundles
    validators.py       invariant checks on a bundle
    serialize.py        JSON-safe, canonical serialisation
    textutil.py         dates, money, account IDs, conservative name/address keys
evidence_intelligence/  Person 2 - fact extraction
    models.py           CaseFacts, AlertFacts, RiskIndicator, DocumentMatch, ListMatchCandidate, ...
    documents.py        type-aware document facts (parties, amounts, accounts, references, registers, signals)
    metrics.py          Decimal aggregates, windows, rapid movement, clusters, dormancy
    document_matching.py document <-> transaction links, graded field by field
    list_matching.py    internal-list candidates (name / DOB / address / control timeline)
    rules.py            one analyser per rule: TM01-TM08, TM10, TM22
    reviews.py          previous-review applicability
    provenance.py       citation checks
    extractor.py        FactExtractor: orchestration
    handoff/
      integration.py    inputs for Policy Intelligence (Person 3) and Decision Intelligence (Person 4)
      example.py        example integration script
      cli.py            diagnostic facts export (not the submission)
tests/                  unittest suite (synthetic fixtures + real-data integration)
```

**Placement notes.**
- **POLICY.md parsing stays in `data_processing/policy.py`.** It reads a source file into the same typed models as the rest of ingestion, and the case repository depends on it, so moving it to `policy_intelligence/` would create a circular import between the packages. Policy Intelligence consumes its output through `build_policy_retrieval_input` and `bundle.policy`.
- **Nothing was added to `decision_intelligence/`.** These modules contain no decision logic.
- **The teammate file `evidence_intelligence/fact_extractor.py` is left untouched** and can still be imported as `evidence_intelligence.fact_extractor`.

## Setup

1. Install Python 3.11 or newer; no packages are needed. On Windows you can use `py -3` in place of `python` in the commands below.
2. Tell the code where the dataset is. It looks in this order:
   - the `AFC_INSTITUTIONS_DIR` environment variable, pointing at the folder that holds `bank/`, `emoney/`, `investment/` and `payments_fx/`;
   - `<repo>/institutions`;
   - `<repo>/../hackathon/institutions`, which is the extracted hackathon zip sitting next to this repository.

   To set the variable for the current session:

   PowerShell:
   ```bash
   $env:AFC_INSTITUTIONS_DIR = "C:\path\to\hackathon\institutions"
   ```
   bash:
   ```bash
   export AFC_INSTITUTIONS_DIR=/path/to/hackathon/institutions
   ```
   The data is never copied into the repository; `.gitignore` excludes `institutions/`.

## Running

From the repository root:

```bash
python -m unittest discover -s tests -t .
```

```bash
python -m evidence_intelligence.handoff.cli --institution bank --output facts_bank.jsonl
```

```bash
python -m evidence_intelligence.handoff.example payments_fx 2
```

`--institution` accepts a full folder path, or a name (`bank`, `emoney`, `investment`, `payments_fx`) that is looked up through the dataset location above. The CLI runs from any working directory. It refuses to write to `output/<institution>.jsonl` unless you pass `--force`. Other options: `--case-id` (repeatable), `--limit N`, `--include-bundle` and `--include-retrieval`.

## Public API

```python
from data_processing import CaseRepository, load_institution, validate_case_bundle, serialize_case_bundle
from data_processing.config import institutions_dir, resolve_institution
from evidence_intelligence import FactExtractor, extract_case_facts, serialize_case_facts, validate_evidence_ids, ProvenanceIndex
from evidence_intelligence.handoff import process_institution, build_policy_retrieval_input, build_reasoning_input

repo = CaseRepository(institution_path: str | Path)        # loads and indexes everything once
repo.get_case_bundle(case_id: str) -> CaseBundle            # raises CaseNotFound for an unknown ID
repo.iter_case_bundles() -> Iterator[CaseBundle]            # in cases.jsonl order
repo.policy -> PolicyParameters                             # .get("TM03.band_low_gbp"), .high_risk_countries, .runs("TM10")
validate_case_bundle(bundle) -> list[DataQualityIssue]
serialize_case_bundle(bundle, *, include_document_text=True) -> dict

FactExtractor().extract(bundle) -> CaseFacts                # same as extract_case_facts(bundle)
serialize_case_facts(facts) -> dict

process_institution(path) -> Iterator[tuple[CaseBundle, CaseFacts]]
build_policy_retrieval_input(bundle, facts) -> PolicyRetrievalInput   # for Person 3
build_reasoning_input(bundle, facts) -> ReasoningInput                # for Person 4
validate_evidence_ids(ids, ProvenanceIndex(bundle)) -> {"valid", "alert_ids_not_evidence", "unknown"}
```

The file loaders are also exported: `load_cases`, `load_alerts`, `load_customers`, `load_transactions`, `load_evidence_index`, `load_evidence_document`, `load_previous_reviews` and `load_internal_list`. Each takes the institution path and an optional list that collects issues.

## Normalised case bundle (`schema_version` 1.0)

`serialize_case_bundle` gives, per case:

```json
{
  "schema_version": "1.0", "case_id": "PX-CS100487", "institution": "payments_fx", "customer_id": "PX-C709225",
  "opened_at": "2026-08-05T12:01:00Z", "review_window": {"from": "2026-07-10", "to": "2026-08-08"},
  "alert_ids": ["PX-AL9165462"], "held_payment_id": "PX-T596773223",
  "customer": {"identity": {...}, "account_ids": [...], "risk_rating": "Low", "declared_profile": {...}, "raw_source": {...}},
  "alerts": [{"alert_id", "rule_id", "rule_name", "created_at", "message", "measured", "transaction_ids", "raw_source"}],
  "transactions": [{"transaction_id", "booked_at", "direction", "channel", "amount_gbp", "amount", "currency",
                    "counterparty_name", "counterparty_account", "counterparty_country", "reference", "location",
                    "segment": "pre_window|window|post_window", "alert_ids": [...], "is_held_payment": true, "raw_source": {...}}],
  "evidence_documents": [{"doc_id", "type", "title", "dated", "relative_path", "media_type": "text/markdown",
                          "read_status": "ok", "parsed": {"fields", "tables", "paragraphs"}, "extraction_method",
                          "raw_text", "raw_source"}],
  "previous_reviews": [...], "internal_list_candidates": [{"entry_id", "included_because": [...], "entry": {...}}],
  "policy": {"rules", "high_risk_countries", "parameters": {"TM03.band_low_gbp": {"value": "9000.00", "source": "POLICY.md", "evidence": "..."}}},
  "data_quality": {"missing_references": [], "warnings": [], "errors": []}
}
```

- **Transactions.** The bundle holds *all* of the customer's transactions (on average about 90 days before the window, and up to about 500 days for dormant accounts), each tagged with a segment. Lookbacks are applied where they are needed: 180 days for dormancy, 90 for TM10, older payments for refunds. A transaction cited by several alerts appears once, with all of its `alert_ids`.
- **Internal list.** Only the relevant entries are attached: those named by an alert, plus exact normalised-name hits against window counterparties and register officers. The full list stays on `repo.internal_list`.
- **Numbers and dates.** Every monetary value is a `Decimal`. Serialised, it becomes a string of the exact original digits (for example `"12923.57"`), including inside `raw_source`. Timestamps are UTC (`...Z`).

## Case facts (`schema_version` 1.0)

Top-level keys: `schema_version`, `case_id`, `institution`, `customer_id`, `alert_ids`, `held_payment_id`, `review_window`, `customer_profile`, `transaction_summary`, `alert_facts`, `risk_indicators`, `supporting_explanations`, `contradicting_evidence`, `document_facts`, `document_matches`, `source_of_funds`, `internal_list_matches`, `previous_review_applicability`, `unresolved_questions`, `data_quality` and `evidence_index`.

Here is an excerpt from a real case, `BK-CS125943` (TM22: the alert matched a company officer's name and month of birth):

```json
{"alert_id": "BK-AL2932422", "rule_id": "TM22",
 "recomputed": {"candidate_confidence": "probable", "matched_fields": ["month_year_of_birth", "name"],
   "control_relationship": {"status": "in_role_on_payment_date",
     "detail": "Emery Draycote controller of Elmcote Consulting Ltd (60%); in role on payment date 2026-09-15 per PSC-5155",
     "roles": [{"doc_id": "PSC-5155", "kind": "controller", "status": "in_role_on_payment_date", "end": "2026-09-17"},
               {"doc_id": "REG-6142", "kind": "officer", "status": "role_ended_before_payment", "end": "2026-09-14"}]}},
 "unresolved_requirements": [{"requirement": "whether the counterparty is, or is controlled by, the listed party",
                              "status": "listed person held a role at the counterparty on the payment date"}]}
```

What each part holds:

- **`alert_facts[]`** (one per alert):
  - `measured`: the supplied values, unchanged;
  - `recomputed`: an independent recalculation with its window and method;
  - `discrepancies`: supplied vs recomputed, with an explanation;
  - `observations`: rule-specific facts;
  - `candidate_explanations`: document matches and other supporting records;
  - `unresolved_requirements`: what is still unexplained;
  - `coverage`: evidence status per alerted transaction, plus totals.
- **`risk_indicators[]`** are descriptive signals, never verdicts. Each has `indicator_id`, `rule_id`, `indicator_type`, `observed_value`, `threshold`, `result` (true / false / null for unknown), `calculation`, `transaction_ids`, `supporting_ids`, `alert_ids`, `caveats`, `verification_status` and a `heuristic` flag. Phrase-based signals (for example "asked about thresholds" or "sends it on for a manager") quote the exact sentence and are marked `heuristic`.
- **`document_matches[]`** each have `match_id`, `doc_id`, `transaction_ids`, `match_kind` (single or aggregate), `matched_fields`, `partial_fields`, `missing_fields`, `conflicting_fields`, `comparisons`, `strength` (strong / moderate / weak / conflicting), `verification_status` and `explanation`.
- **`source_of_funds`** gives a status (`verified`, `partially_supported`, `claimed_only`, `missing`, `contradicted` or `unknown`) for the inflows the alerts cite, both per inflow and overall. A documented materiality rule leaves routine small inflows out of the overall status.
- **`internal_list_matches[]`** each have `entry_id`, `matched_fields`, `conflicting_fields`, `unknown_fields`, `comparisons`, `control_relationship`, `candidate_confidence` and `supporting_ids`.
- **`previous_review_applicability[]`** gives, per review, `covers_current_activity` (`no` or `not_established`) with reasons, following the policy that an earlier review covers only what it describes.

### Evidence-status vocabulary

| Status | Meaning |
|---|---|
| `verified` | A third-party or institution document reconciles: exact amount plus account or reference, or plus counterparty and date. No field conflicts. |
| `partially_supported` | Only partial support, or a customer-prepared record (takings book, collection report, payment file). |
| `claimed_only` | Only the customer's own words: a chat, or an email or letter from the customer. |
| `missing` | Nothing on file addresses it. This is **not** evidence of wrongdoing. |
| `contradicted` | A document conflicts on payee, account or reference, or a fraud report matches the inflow. |
| `unknown` | Cannot be assessed from the data. |

### Document-match grading

- **Proposing a link.** A link is proposed only when an amount is exactly equal, or equal to the sum of a group of transactions, or when an alerted transaction shares a reference number with the document. Approximate amounts (within 0.5%) count only where a currency conversion is involved.
- **Name similarity.** A name that only looks similar never lifts a match above `moderate`.
- **Wrong payee.** An invoice whose amount matches but whose payee differs is graded `conflicting`, not reconciled.
- **Cash.** Cash has no counterparty. So an exact amount and date against a record of that cash (a collection report, or a sale paid in cash) counts as `strong`. If the record is customer-prepared, its status is still only `partially_supported`.

### List-match confidence

| Confidence | When |
|---|---|
| `identifier_conflict` | Date of birth (or month and year from a register) differs from the list. Supports "different party". |
| `address_premises_differ` | Same building but a different unit, e.g. "Unit 4A" vs "Unit 4B". Supports "different party". |
| `identity_corroborated` | Name plus an equal full date of birth. |
| `probable` | Name plus equal month and year of birth, or name plus the same address. |
| `possible` | One detail only. A name-only match lands here. |
| `none` | Nothing on file beyond the alert text. |

`control_relationship` compares a register officer's or controller's start and end dates with the payment date.

## Evidence provenance

- **Citation check.** Every ID a fact cites is checked against the case's own scope: the customer's transactions and documents, the case's alerts, the attached list entries, and the customer's reviews. `data_quality.unresolved_citations` lists any that fail; it was empty for all 4,000 development cases.
- **Evidence index.** `evidence_index` describes each citable ID and says which kinds may appear in a submission's `evidence` field. Transaction, document, list-entry and review IDs may; alert IDs are kept separately for traceability.
- **Person 4's check.** Run `validate_evidence_ids()` on any evidence list before writing it.

## Connecting Person 3 (policy retrieval)

`build_policy_retrieval_input(bundle, facts)` returns:

| Field | Contents |
|---|---|
| `institution` | Name of the institution folder. |
| `triggered_rule_ids` | The rules that fired in the case. |
| `rule_definitions` | Each rule's "fires when" text from POLICY.md. |
| `policy_parameters` | The parsed limits that apply. |
| `observed_metrics` | Measured vs recomputed values, per alert. |
| `evidence_statuses` | Source of funds, coverage, held-payment invoice requirement, list confidence, and the own-account five-times signal. |
| `missing_requirements` | What is still unexplained. |
| `query_hints` | Short retrieval queries, for example "payments_fx held payment invoice or contract before release". |

Index the four POLICY.md files. For each case, use `triggered_rule_ids` plus `query_hints` to pull the matching rule rows and policy settings.

## Connecting Person 4 (reasoning)

`build_reasoning_input(bundle, facts)` returns:

| Field | Contents |
|---|---|
| `case_bundle` | The full normalised case, including document text. |
| `facts` | The extracted case facts. |
| `supporting_evidence` | Strong or moderate matches, different-party list findings, explanatory signals. |
| `contradicting_evidence` | Conflicting matches, fraud reports, risk phrases, identity-corroborated or in-control list findings. |
| `per_alert_coverage` | Evidence coverage for each alert. |
| `provenance_index` | Description of every citable ID. |
| `allowed_evidence_ids` | The only IDs a submission line may cite. |

Person 4 maps all of this to CLOSE / RFI / ESCALATE and a reason code. Things Person 4 should know:

- **One verdict per case.** A case can close only when every alert's coverage and unresolved requirements allow it.
- **Payments and FX invoice rule.** `data_quality.held_payment_requirements` says whether the £10,000 invoice-or-contract rule applies to the held payment, and whether it is met.
- **Own-account five-times rule.** `alert_facts[TM01].observations.own_account_policy` and the per-transaction `ownership_evidence` carry what the rule needs: the activity multiple compared with 5×, and whether a document proves the customer holds the other account.

## How the dataset differs from the brief's assumptions

| Area | What the data actually shows |
|---|---|
| Evidence format | Every document is Markdown (no PDFs). Each file repeats its `Document ID` and `Dated` in a header; all 16,402 agree with the index. |
| Cases and accounts | Each customer has exactly **one** case and **one** account. |
| Held payments | Only payments_fx has them (489 cases). Each is outgoing and cited by an alert. |
| Previous reviews | All 1,528 are `CLOSE`, with short notes and no covered transactions or periods. Applicability can therefore use only rule, date and names, and is marked context-only. |
| Internal-list identifiers | Only `internal_case` and one `swift` value; no company numbers. Registers give **month and year** of birth; gift letters give full dates. |
| Business turnover (bank) | Businesses declare `annual_turnover_gbp`, while POLICY.md speaks of monthly turnover, so the baseline is annual / 12, labelled as derived. |
| Payments FX TM01 baseline | Annual volume / 12, because the policy explicitly calls it a monthly share. |
| Which rules run where | Taken from each POLICY.md, not assumed: TM08 doesn't run at the bank or e-money firm, TM03 and TM04 don't run at the investment firm, TM05 runs only at the bank and e-money firm, and TM10 only at the investment firm. |
| Measurement conventions | Calibrated against the supplied `measured` values, and all 6,467 alerts now match: windows are inclusive N-day windows ending on the date in the alert message; TM02 counts window outflows booked within 48 h after a window inflow; investment "invested" is fund purchases dated in the window. |
| Documents dated after the case opened | 238 documents. Reported as `info`, not as an error. |
| Missing or broken references | None in the development data. The loaders and tests still cover them. |

## Conservative assumptions and known limitations

- **Document extraction** is pattern-based. All 54 document types in the data have an explicit classification. An unknown type is classified from its title, and if that fails it stays `unknown` (reported in `extraction_notes`) rather than being guessed.
- **Narrative signals** come from a fixed phrase list (`evidence_intelligence/documents.py::_SIGNALS`) and are flagged `heuristic`. They will miss paraphrases. Person 4, or an LLM, should read the quoted text.
- **City-to-country gazetteer.** A small list for the policy's higher-risk countries, marked heuristic. An ambiguous city (Tripoli) is never mapped.
- **Name normalisation** removes legal suffixes and punctuation only. Joint-account and family links (shared surnames) are reported as heuristic candidates with status `unknown`, never as proof.
- **Matching ambiguity.** Exact-amount matching can link a document to the wrong one of several equal transactions. Matches that conflict with a document already strongly matched elsewhere are dropped; any others are reported as conflicting, never resolved silently.
- **Rapid-movement allocation** is arithmetic, both FIFO and by horizon. It never claims that a particular inflow funded a particular outflow.
- **Optional formats.** PDF reading only happens if `pypdf` is installed; the supplied data never needs it. The CSV reader is provided but not used.

## Test results and performance

- **Tests:** 62 pass (`python -m unittest discover -s tests -t .`), on Python 3.12.10 under Windows 11. They cover:
  - **Loaders:** Decimal amounts, timezones, lossless raw values, corrupt JSON, naive timestamps, duplicate records, missing files, path traversal.
  - **Case assembly:** case linking, deduplication, missing and foreign references, held payments, history, no cross-customer contamination, list scoping.
  - **Metrics:** windows, FIFO allocation, clusters, dormancy.
  - **Rules:** institution-specific differences for TM01, TM02, TM03, TM05, TM06, TM07 and TM10, and "no verdicts in the facts".
  - **Document matching:** verified vs conflicting vs name-only vs moderate vs claimed-only, aggregate instalments, cash records, FX-only tolerance.
  - **List matching:** different DOB, control timeline, name-only, unit differs, gift-letter DOB.
  - **Provenance** checks.
  - **Real data:** the first 40 cases per institution, checked for determinism and citations; the CLI from another working directory; the refusal to write to a submission path.
- **Full development set:** all 4,000 cases, 6,467 alerts and 16,402 documents processed, with 0 bundle errors, 0 unresolved citations and 0 measurement discrepancies. Two separate runs gave identical SHA-256 hashes of the canonical facts.
- **Speed and memory:** loading takes 0.5–2.5 s per institution. Facts take 10–20 ms per case once files are cached; the first cold read is 2–3 times slower. **200 cases including the load take 2–6 s.** Peak traced Python memory for the bank load plus 200 cases is about 190 MiB.
