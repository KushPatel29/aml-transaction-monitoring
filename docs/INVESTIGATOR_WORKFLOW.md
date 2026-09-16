# Governed investigator workflow

`AML-INV-01` turns detection output into a controlled, reviewable case workflow. It deliberately stops before a human compliance decision.

## Decision chain

1. The existing detector scores synthetic transactions without using planted labels as features.
2. The operating threshold consolidates transaction alerts to one investigation per entity.
3. A versioned typology catalogue states the hypothesis, lawful lookalike, discriminator, minimum evidence and known limitation for each rule.
4. The case register assigns P0/P1/P2 review clocks and a next evidence step. Priority and workflow state depend only on monitoring evidence, never on planted ground truth.
5. The entity graph aggregates entity-counterparty edges and shared-entity degree. It supports inquiry; density is not represented as proof.
6. Eight release gates verify completeness, label quarantine, graph integrity, explainability, SLA assignment and capacity. Human disposition and reporting approval remain REVIEW gates.
7. An in-memory missing-case drill proves the release fails closed from REVIEW REQUIRED to BLOCKED.

## Outputs

- `results/investigation_case_register.csv` — one controlled investigation per alerted entity.
- `results/investigation_entity_graph.csv` — aggregated entity-counterparty edges.
- `results/typology_evidence_catalogue.csv` — evidence standards and lawful lookalikes.
- `results/investigation_release_gates.csv` — the executable release docket.
- `results/investigation_decision_packet.md` — concise reviewer brief.
- `results/investigation_manifest.json` — input hashes, decision boundary and register fingerprint.
- `results/investigator_reverification_evidence.json` — controlled fail-closed proof.

Rebuild with:

```bash
python -m governance.investigator_workflow
```

## Boundary

Every record is synthetic. The workflow is not a compliance product, legal conclusion, customer decision, suspicious transaction report or regulatory filing. Only an authorized human operating inside a real institution's controls can make those decisions.
