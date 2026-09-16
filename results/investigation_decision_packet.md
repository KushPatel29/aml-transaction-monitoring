# AML-INV-01 Investigator Decision Packet

> Synthetic portfolio evidence. This is not a compliance product, legal conclusion, customer decision, suspicious transaction report, or regulatory filing.

## Release decision

**REVIEW REQUIRED** — 6 gates pass, 2 require review, and 0 block.

The register contains **366 investigations** (78 P0, 11 P1, 277 P2), **8972 graph edges** and **5 governed typology hypotheses**.

## Gate docket

| Gate | Status | Observed |
|---|---|---|
| QUEUE-01 · One investigation per alerted entity | **PASS** | 366 registered / 366 expected |
| LABEL-01 · Ground truth quarantined from investigator workflow | **PASS** | 0 evaluation-label columns in the case register |
| GRAPH-01 · Entity graph referential integrity | **PASS** | 8972 entity-counterparty edges linked to registered cases |
| REASON-01 · Explainability boundary | **PASS** | 277 model-only cases remain explicitly unclassified |
| SLA-01 · Priority and review clock | **PASS** | P0/P1/P2 cases carry 4/24/72-hour review clocks |
| CAP-01 · Illustrative analyst capacity | **PASS** | 7.0 alerts/week against 48.0 capacity |
| HUMAN-01 · Human disposition approval | **REVIEW** | 0 authorized human dispositions recorded |
| FILING-01 · Regulatory reporting decision | **REVIEW** | 0 reporting recommendations approved or filed |

## Human decision boundary

Only an authorized human investigator may close a case, escalate enhanced due diligence, or recommend a regulatory filing. This repository records no such approval or filing.

The public workflow records zero human dispositions and zero regulatory filings. Risk scores and rules prioritize evidence; they do not determine suspicion or authorize action.

## Re-verification proof

Removing one case in memory moves QUEUE-01 from **PASS** to **BLOCK** and the release from **REVIEW REQUIRED** to **BLOCKED** without mutating a source file.

## Required disposition

1. Validate evidence for each P0 case inside its review clock.
2. Record an authorized human disposition in the institution's controlled case-management system.
3. Keep regulatory reporting decisions outside this public demonstration.
4. Rebuild after any policy, threshold, feature, rule, model or source-evidence change.
