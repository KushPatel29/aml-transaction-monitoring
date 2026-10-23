# Privacy impact assessment

The assessment a Canadian reporting entity would need before running this system on real clients.
The data here is synthetic, so no real person's information is at risk; the point is to show that
every element the system holds has a purpose, a legal footing, a retention rule and an owner, and
to say plainly which risks the code already handles and which a production deployment would still
have to.

It follows the structure of a privacy impact assessment under the *Personal Information Protection
and Electronic Documents Act* (PIPEDA): what is collected and why, where it flows, an assessment
against PIPEDA's ten fair information principles, and a risk register with mitigations. The data
inventory behind it is machine-readable,
[`governance/privacy_inventory.json`](../governance/privacy_inventory.json), and
[`tests/test_privacy_inventory.py`](../tests/test_privacy_inventory.py) fails the build if a file
or a column appears that the inventory does not classify.

## 1. The system and its purposes

Transaction monitoring, know-your-client risk rating, beneficial ownership, sanctions screening and
case management for a bank or credit union (a "reporting entity" under the *Proceeds of Crime (Money
Laundering) and Terrorist Financing Act*, PCMLTFA). Its purposes are the Act's obligations as
FINTRAC describes them ([regulatory mapping](CANADIAN_REGULATORY_MAPPING.md)):

| Purpose | What it needs |
|---|---|
| Detect and report suspicious transactions and large cash transactions | transactions, the parties to them, monitoring outputs |
| Assess and document each client's money laundering and terrorist financing risk | KYC profile: PEP status, adverse media, onboarding channel, declared and actual inflows |
| Identify beneficial owners at 25% or more | ownership links between clients |
| Screen clients against sanctions | client names, against Canada's Consolidated Autonomous Sanctions List |
| Keep records FINTRAC can ask for | all of the above, retained and retrievable |

**Legal footing.** PIPEDA permits a reporting entity to disclose personal information to FINTRAC
without the client's knowledge or consent when it reports as the PCMLTFA requires, and the Privacy
Commissioner's guidance is equally plain that the Act is not licence to collect more: collect only
what is needed to comply, and report only the personal information that is required. FINTRAC's
record-keeping guidance sets the floor on retention: at least five years (from the record's
creation for a large cash transaction record, after submission for a suspicious transaction
report), longer allowed, shorter not, and retrievable within 30 days of a request.

## 2. What it holds

105 columns across 14 files, each in one of 9 classes
([inventory](../governance/privacy_inventory.json)):

| Class | Personal | Sensitivity | Examples | On screen |
|---|---|---|---|---|
| Direct identifier | yes | high | `display_name` | initials only |
| Party reference | yes | medium | `entity_id`, `counterparty_id`, owners | prefix and last four characters |
| Event reference | no | low | `transaction_id`, `investigation_id` | in full |
| Financial activity | yes | high | amounts, timing, channel, derived features | beside masked parties |
| Account attribute | yes | low | client type, segment, coarse region, account dates | shown |
| KYC assessment | yes | high | PEP status, adverse media, risk ratings, EDD flags, ownership shares | analysts only |
| Monitoring output | yes | high | rule hits, scores, case state, next step | analysts only |
| Synthetic ground truth | n/a | | `is_suspicious`, `typology`, the planted cases | never |
| Generator internal | n/a | | `payroll_base_cad`, `rent_cad` | never |

The last two classes exist only because the data is synthetic. A production system would not have
an answer key, and would not collect a client's pay or rent for monitoring; the generator uses them
to make behaviour realistic. Neither reaches an investigator output (tested).

No government identifier, account number or card number is held, and a test scans every committed
file for anything shaped like one ([`test_no_pii_in_data.py`](../tests/test_no_pii_in_data.py)).

## 3. How it flows

```
generator (synthetic)  ->  SQLite warehouse  ->  rules + anomaly model  ->  scored transactions
                                                                         ->  alerts by client
KYC profiles + ownership  ->  risk rating, EDD flags, beneficial owners
client names              ->  sanctions screen (list held locally, never committed)
alerts + KYC              ->  case register, entity graph, decision packet  ->  HUMAN DECISION REQUIRED
                                                                             ->  (a person decides whether to report)
transactions (stream)     ->  Kafka topics on a CI cluster  ->  streaming rules  ->  alerts topic
```

Nothing leaves for FINTRAC automatically: every case ends at `HUMAN DECISION REQUIRED` and
`NOT ASSESSED OR FILED`, and the filing decision is a person's.

## 4. Against PIPEDA's ten principles

| Principle | Finding | Risks |
|---|---|---|
| 1. Accountability | The design names a decision owner for every case; the inventory names an audience for every file. A production deployment needs a named privacy officer alongside the AML compliance officer. | P6 |
| 2. Identifying purposes | Every class maps to a PCMLTFA purpose (section 1); the two that do not are synthetic-only. | P1 |
| 3. Consent | Reporting to FINTRAC needs no consent; collection for KYC is a condition of the account and belongs in the client privacy notice. | P9 |
| 4. Limiting collection | No identifier numbers are held; generator-only fields are labelled as such. | P1 |
| 5. Limiting use, disclosure and retention | Monitoring outputs stay with analysts; the published register carries initials only; retention is set per file, but nothing yet deletes after it. | P3, P7 |
| 6. Accuracy | KYC ratings are re-assessed at a periodic refresh with twelve months of activity, and every client has a next review date. | P12 |
| 7. Safeguards | Masking on every rendered table, tested; no authentication on the public demo. | P2, P10 |
| 8. Openness | The methods, thresholds and their limits are public in this repository; a client-facing notice is not written. | P9 |
| 9. Individual access | An access request that touches an alert must not reveal a report; that is a procedure, not code. | P6 |
| 10. Challenging compliance | Every rating carries its reasons and every rule alert its reason codes (a model-only alert says it has none), so a decision can be explained and contested. | P5 |

## 5. Risk register

| ID | Risk | Rating | Mitigation | Status | Where |
|---|---|---|---|---|---|
| P1 | Collecting more than the Act needs | Medium | Every column classified and mapped to a purpose; generator-only fields named and never displayed | In place | [`privacy_inventory.json`](../governance/privacy_inventory.json), [`test_privacy_inventory.py`](../tests/test_privacy_inventory.py) |
| P2 | Full identifiers on an analyst's screen next to a risk score | High | Every table the dashboard renders goes through `mask_frame`: initials for names, prefix and last four for party keys | In place | [`src/masking.py`](../src/masking.py), [`test_dashboard_masks_identifiers.py`](../tests/test_dashboard_masks_identifiers.py) |
| P3 | Full names in the published case register | High | The register is written with initials, the dashboard's rule; the full name stays in the client file | In place (this assessment) | [`investigator_workflow.py`](../governance/investigator_workflow.py), [`test_privacy_inventory.py`](../tests/test_privacy_inventory.py) |
| P4 | The answer key leaking into what investigators see | High | Ground-truth columns are excluded from the register, the graph and the KYC outputs | In place | [`test_investigator_workflow.py`](../tests/test_investigator_workflow.py), [`test_privacy_inventory.py`](../tests/test_privacy_inventory.py) |
| P5 | Automated decisions about people without review or reasons | High | No automatic filing or blocking; every case ends at a human decision; every KYC rating carries its reasons, every rule alert its reason codes, and a model-only alert is marked as having none | In place | [`src/kyc.py`](../src/kyc.py), [`investigator_workflow.py`](../governance/investigator_workflow.py) |
| P6 | Tipping off through an access request, or no one accountable for privacy | High | Route any access request that touches an alert or a report to the AML compliance officer before answering; name a privacy officer | Open: procedure | this document |
| P7 | Keeping data longer than needed | Medium | Retention set per file at FINTRAC's five-year floor; a destruction schedule after it is still to be built, and the streaming demo keeps its transaction topic forever (`retention.ms = -1`), which a production topic must not | Open | [`privacy_inventory.json`](../governance/privacy_inventory.json), [`streaming/kafka_io.py`](../streaming/kafka_io.py) |
| P8 | A sanctions false positive harming a client | Medium | Fuzzy-match threshold set on a held-out half; a match is an alert for review, never an automatic block | In place | [`src/sanctions_screening.py`](../src/sanctions_screening.py) |
| P9 | Clients not told how monitoring uses their data | Medium | A client privacy notice covering monitoring, KYC and reporting, in plain language | Open: notice | not written |
| P10 | Anyone with the link can open the dashboard | High in production | Acceptable for a public demo on synthetic data; production needs single sign-on and role-based access (analyst, supervisor, auditor) | Open | [`app/streamlit_app.py`](../app/streamlit_app.py) |
| P11 | Personal information stored outside Canada | Medium | Decide hosting region before production and say so in the notice | Open | not decided |
| P12 | Stale KYC data driving a rating | Medium | Periodic refresh with twelve months of activity; next review date per client | In place | [`src/kyc.py`](../src/kyc.py) |

**Residual risk.** 7 of 12 risks are handled in code and tested. The five open ones are
procedures, a notice, access control and retention enforcement: the parts a privacy officer and an
infrastructure team would own, and the parts a portfolio on synthetic data cannot honestly claim to
have done.

## Sources

- Office of the Privacy Commissioner of Canada,
  [Frequently asked questions: the PCMLTFA and PIPEDA](https://www.priv.gc.ca/en/privacy-topics/surveillance/police-and-public-safety/financial-transaction-reporting/faqs_pcmltfa_02/)
- FINTRAC, [Record keeping requirements for financial entities](https://fintrac-canafe.canada.ca/guidance-directives/recordkeeping-document/record/fin-eng)
- Office of the Privacy Commissioner of Canada,
  [PIPEDA fair information principles](https://priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/p_principle)
