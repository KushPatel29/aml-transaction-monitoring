"""Check evidence completeness before human review; never adjudicate a case."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "results/investigation_case_register.csv"
GRAPH = ROOT / "results/investigation_entity_graph.csv"
PROHIBITED = {"is_suspicious", "typology", "typology_variant", "case_id", "ground_truth"}
RECOMMENDATIONS = {"request_information", "escalate_for_review", "propose_closure"}


def _keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


def evidence():
    with REGISTER.open(newline="", encoding="utf-8") as stream:
        cases = {row["investigation_id"]: row for row in csv.DictReader(stream)}
    with GRAPH.open(newline="", encoding="utf-8") as stream:
        graph = list(csv.DictReader(stream))
    return cases, graph


def assess(note: dict, cases: dict, graph: list[dict]) -> dict:
    problems = []
    case = cases.get(note.get("investigation_id"))
    if note.get("schema_version") != 1:
        problems.append("unsupported note schema")
    if _keys(note) & PROHIBITED:
        problems.append("evaluation labels cannot enter the reviewer note")
    if case is None:
        problems.append("unknown investigation")
    if note.get("recommendation") not in RECOMMENDATIONS:
        problems.append("unsupported recommendation; no final disposition or filing allowed")
    valid_refs = set()
    if case:
        valid_refs.add("register:" + case["investigation_id"])
        valid_refs.update(
            "counterparty:" + row["counterparty_id"]
            for row in graph
            if row["entity_id"] == case["entity_id"]
        )
    facts = note.get("observed_facts", [])
    if not isinstance(facts, list) or not facts:
        problems.append("observations with source references are required")
    else:
        for fact in facts:
            if not isinstance(fact, dict) or not str(fact.get("observation", "")).strip():
                problems.append("empty observation")
            if not isinstance(fact, dict) or fact.get("source_ref") not in valid_refs:
                problems.append("unknown or cross-case evidence reference")
    for field in ("alternative_explanation", "discriminator", "analysis", "recommended_next_step"):
        if not isinstance(note.get(field), str) or not note[field].strip():
            problems.append("missing " + field)
    missing = note.get("missing_evidence")
    if not isinstance(missing, list) or any(
        not isinstance(item, str) or not item.strip() for item in missing
    ):
        problems.append("missing_evidence must explicitly list unresolved information")
    if note.get("recommendation") == "propose_closure" and missing:
        problems.append("closure proposal has unresolved evidence")
    if note.get("human_approved") is not False or note.get("filing_status") != "NOT FILED":
        problems.append("the training note cannot assert human approval or a filing")
    return {
        "investigation_id": note.get("investigation_id"),
        "structural_quality": "BLOCKED" if problems else "READY FOR HUMAN REVIEW",
        "problems": sorted(set(problems)),
        "decision_authorized": False,
        "independent_review": "PENDING",
        "boundary": (
            "Completeness and reference checks do not verify narrative truth, determine "
            "suspicion, close a case or meet legal reporting obligations."
        ),
    }


def training_note(cases, graph):
    case = next(row for row in cases.values() if row["rules_fired"] == "R2_LAYERING")
    return {
        "schema_version": 1,
        "classification": "synthetic_training_draft",
        "investigation_id": case["investigation_id"],
        "observed_facts": [
            {
                "observation": case["evidence_references"],
                "source_ref": "register:" + case["investigation_id"],
            }
        ],
        "alternative_explanation": (
            "A wholesale receipt followed by a scheduled supplier-payment batch could "
            "produce a similar fund flow."
        ),
        "discriminator": (
            "Compare invoices, the payment schedule, counterparty purpose and "
            "the expected customer profile."
        ),
        "analysis": (
            "The observed rapid movement warrants clarification. The current analytical "
            "register does not establish the economic purpose or source of funds; "
            "its score is prioritization evidence only."
        ),
        "missing_evidence": [
            "invoice support",
            "source of funds",
            "counterparty purpose",
            "expected activity",
        ],
        "recommendation": "request_information",
        "recommended_next_step": (
            "Request the missing support, reconcile the fund-flow timeline, "
            "and route the response to an authorized reviewer."
        ),
        "human_approved": False,
        "filing_status": "NOT FILED",
    }


def build():
    cases, graph = evidence()
    note = training_note(cases, graph)
    return {
        "schema_version": 1,
        "classification": "synthetic_training_quality",
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for path in (REGISTER, GRAPH)
        },
        "training_note": note,
        "quality": assess(note, cases, graph),
        "human_reviews_recorded": 0,
        "case_dispositions_recorded": 0,
        "regulatory_filings_recorded": 0,
    }


if __name__ == "__main__":
    packet = build()
    (ROOT / "results/review_quality.json").write_text(
        json.dumps(packet, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(packet["quality"], indent=2))
