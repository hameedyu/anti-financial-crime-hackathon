# anti-financial-crime-hackathon
Review alerts, check the customers profile, transactions etc and make a decision
Anti-Financial-Crime Hackathon
Architecture
Case ↓ Data Processing ↓ Evidence Intelligence ↓ Policy Intelligence ↓ Decision Intelligence ↓ Verdict + Evidence

Team Responsibilities
Data Processing
Evidence Intelligence
Policy Intelligence
Decision Intelligence
Frontendgit 

## Setup and running (Data Processing + Evidence Intelligence)

Requirements: Python 3.11+ (standard library only; on Windows `py -3` works in place of `python`). The hackathon dataset is kept outside this repository. Set `AFC_INSTITUTIONS_DIR` to its `institutions` folder, or extract the hackathon zip next to this repository as `../hackathon/`.

| Folder | Contents |
|---|---|
| `data_processing/` | Loads, validates and indexes an institution folder into case bundles (`CaseRepository`) |
| `evidence_intelligence/` | Extracts evidence-backed facts per case (`FactExtractor`); `handoff/` builds the inputs for Policy and Decision Intelligence |
| `tests/` | `unittest` suite |
| `docs/person1_person2.md` | Full API, schemas and design notes |

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
