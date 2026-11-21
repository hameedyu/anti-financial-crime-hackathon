# Prewarning
Working branch: feature/person1-person2-ingestion-facts

The decision system and system/run.py are on this branch, not on main.



# Decision System


This system reads a transaction-monitoring case and produces a single verdict (CLOSE / RFI / ESCALATE) plus a reason code, plain-English reason, and evidence IDs.

## How it decides

- Conservative by default (RFI).
- Escalates on TM22 (internal list match) and on TM02/TM04 (rapid movement or many-to-one patterns).
- RFIs on profile breaches (TM01), cash/near-threshold activity (TM03/TM05), higher-risk corridors (TM06), dormant reactivation (TM07), and third-party activity (TM08).
- Evidence list includes the alert IDs and the transaction IDs that triggered them.

## How to run

```bash
python run.py <institution_folder> <output_file.jsonl>
