"""Build a deterministic, non-decisional AML investigation evidence pack.

The detection pipeline answers which synthetic entities merit review. This
module adds the operational layer that a reviewer needs next: one controlled
case per alerted entity, a typology/evidence catalogue, an entity-counterparty
graph, service levels, release gates and an explicit human decision boundary.

Ground-truth labels remain evaluation-only. They are never copied into the
investigator register or used to select a workflow state or disposition.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "governance" / "investigation_policy.json"
RESULTS = ROOT / "results"

SCORED_PATH = RESULTS / "scored_transactions.csv.gz"
ENTITIES_PATH = ROOT / "data" / "generated" / "entities.csv.gz"
COUNTERPARTIES_PATH = ROOT / "data" / "generated" / "counterparties.csv.gz"
CASES_PATH = ROOT / "data" / "generated" / "cases.csv.gz"
METRICS_PATH = RESULTS / "metrics.json"

REGISTER_PATH = RESULTS / "investigation_case_register.csv"
GRAPH_PATH = RESULTS / "investigation_entity_graph.csv"
TYPOLOGY_PATH = RESULTS / "typology_evidence_catalogue.csv"
GATES_PATH = RESULTS / "investigation_release_gates.csv"
SUMMARY_PATH = RESULTS / "investigation_summary.json"
MANIFEST_PATH = RESULTS / "investigation_manifest.json"
PROBE_PATH = RESULTS / "investigator_reverification_evidence.json"
PACKET_PATH = RESULTS / "investigation_decision_packet.md"

GROUND_TRUTH_COLUMNS = {
    "is_suspicious",
    "typology",
    "typology_variant",
    "case_id",
}


def canonical_sha256(path: Path) -> str:
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "rb") as handle:
            payload = handle.read()
    else:
        payload = path.read_bytes()
    if path.suffix.lower() in {".json", ".csv", ".md", ".py", ".sql"}:
        payload = payload.replace(b"\r\n", b"\n")
    elif path.suffix.lower() == ".gz":
        payload = payload.replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def decision_metrics_sha256(path: Path = METRICS_PATH) -> str:
    """Hash only the operating-point evidence consumed by this workflow."""
    metrics = json.loads(path.read_text(encoding="utf-8"))
    operating_point = metrics["operating_point"]
    entity_level = operating_point["entity_level"]
    decision_evidence = {
        "threshold": operating_point["threshold"],
        "entity_level": {
            "alerts": entity_level["alerts"],
            "alerts_per_week": entity_level["alerts_per_week"],
            "analyst_capacity_per_week": entity_level["analyst_capacity_per_week"],
        },
    }
    payload = json.dumps(
        decision_evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1:
        raise ValueError("Unsupported investigation policy schema_version")
    required = {
        "control_id",
        "policy_version",
        "priority_bands",
        "allowed_states",
        "allowed_dispositions",
        "typologies",
        "human_decision_boundary",
    }
    missing = required - policy.keys()
    if missing:
        raise ValueError(f"Investigation policy missing: {sorted(missing)}")
    return policy


def load_evidence(root: Path = ROOT) -> dict[str, Any]:
    return {
        "scored": pd.read_csv(root / SCORED_PATH.relative_to(ROOT), keep_default_na=False),
        "entities": pd.read_csv(root / ENTITIES_PATH.relative_to(ROOT), keep_default_na=False),
        "counterparties": pd.read_csv(
            root / COUNTERPARTIES_PATH.relative_to(ROOT), keep_default_na=False
        ),
        "cases": pd.read_csv(root / CASES_PATH.relative_to(ROOT), keep_default_na=False),
        "metrics": json.loads(
            (root / METRICS_PATH.relative_to(ROOT)).read_text(encoding="utf-8")
        ),
    }


def _priority(score: float, policy: dict[str, Any]) -> tuple[str, int]:
    for band in sorted(
        policy["priority_bands"], key=lambda item: item["minimum_score"], reverse=True
    ):
        if score >= float(band["minimum_score"]):
            return str(band["priority"]), int(band["due_hours"])
    raise ValueError(f"Score {score} is below every configured priority band")


def _typology_hypothesis(group: pd.DataFrame, policy: dict[str, Any]) -> str:
    rule_hits = []
    for item in policy["typologies"]:
        code = item["rule_code"]
        if code in group and bool(group[code].any()):
            rule_hits.append(item["typology_id"])
    return " + ".join(rule_hits) if rule_hits else "UNCLASSIFIED-MODEL-ONLY"


def build_case_register(
    evidence: dict[str, Any], policy: dict[str, Any]
) -> pd.DataFrame:
    scored = evidence["scored"]
    threshold = float(evidence["metrics"]["operating_point"]["threshold"])
    flagged = scored.loc[scored["risk_score"] >= threshold].copy()
    entity_lookup = evidence["entities"].set_index("entity_id")
    rows: list[dict[str, Any]] = []

    for entity_id, group in flagged.groupby("entity_id", sort=True):
        score = float(group["risk_score"].max())
        priority, due_hours = _priority(score, policy)
        codes = [code for code in (item["rule_code"] for item in policy["typologies"])
                 if code in group and bool(group[code].any())]
        reasons = sorted({str(value) for value in group["reason_codes"] if str(value)})
        profile = entity_lookup.loc[entity_id]
        if priority == "P0":
            state = "SECOND-LEVEL REVIEW"
            next_step = "Validate source of funds and counterparty purpose within four hours."
        elif codes:
            state = "EVIDENCE REQUESTED"
            next_step = "Collect the typology catalogue's minimum evidence before disposition."
        else:
            state = "OPEN TRIAGE"
            next_step = "Establish an explainable hypothesis before any escalation."
        rows.append(
            {
                "investigation_id": f"INV-{entity_id}",
                "entity_id": entity_id,
                "display_name": profile["display_name"],
                "entity_type": profile["entity_type"],
                "segment": profile["segment"],
                "priority": priority,
                "case_state": state,
                "due_hours": due_hours,
                "max_risk_score": round(score, 1),
                "rules_fired": ";".join(codes) if codes else "NONE",
                "typology_hypothesis": _typology_hypothesis(group, policy),
                "flagged_transactions": int(len(group)),
                "amount_involved_cad": round(float(group["amount_cad"].sum()), 2),
                "evidence_references": " | ".join(reasons)[:800]
                if reasons
                else "Model-only anomaly; no rule reason is available.",
                "recommended_next_step": next_step,
                "decision_status": "HUMAN DECISION REQUIRED",
                "filing_status": "NOT ASSESSED OR FILED",
            }
        )
    columns = list(rows[0]) if rows else []
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["priority", "max_risk_score", "investigation_id"],
        ascending=[True, False, True],
    ).reset_index(drop=True)


def build_entity_graph(register: pd.DataFrame, evidence: dict[str, Any]) -> pd.DataFrame:
    alerted = set(register["entity_id"])
    rows = evidence["scored"].loc[
        evidence["scored"]["entity_id"].isin(alerted)
        & evidence["scored"]["counterparty_id"].ne("")
    ].copy()
    if rows.empty:
        return pd.DataFrame(
            columns=[
                "entity_id",
                "counterparty_id",
                "transactions",
                "amount_cad",
                "first_seen",
                "last_seen",
                "max_risk_score",
                "directions",
                "shared_entity_degree",
            ]
        )
    degree = rows.groupby("counterparty_id")["entity_id"].nunique()
    graph = (
        rows.groupby(["entity_id", "counterparty_id"], as_index=False)
        .agg(
            transactions=("transaction_id", "nunique"),
            amount_cad=("amount_cad", "sum"),
            first_seen=("txn_datetime", "min"),
            last_seen=("txn_datetime", "max"),
            max_risk_score=("risk_score", "max"),
            directions=("direction", lambda values: ";".join(sorted(set(values)))),
        )
    )
    graph["shared_entity_degree"] = graph["counterparty_id"].map(degree).astype(int)
    graph["amount_cad"] = graph["amount_cad"].round(2)
    graph["max_risk_score"] = graph["max_risk_score"].round(1)
    return graph.sort_values(
        ["max_risk_score", "amount_cad", "entity_id", "counterparty_id"],
        ascending=[False, False, True, True],
    ).reset_index(drop=True)


def typology_catalogue(policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for item in policy["typologies"]:
        rows.append(
            {
                **{key: value for key, value in item.items() if key != "minimum_evidence"},
                "minimum_evidence": "; ".join(item["minimum_evidence"]),
            }
        )
    return pd.DataFrame(rows)


def release_gates(
    register: pd.DataFrame,
    graph: pd.DataFrame,
    evidence: dict[str, Any],
    policy: dict[str, Any],
) -> list[dict[str, str]]:
    threshold = float(evidence["metrics"]["operating_point"]["threshold"])
    expected_entities = set(
        evidence["scored"].loc[evidence["scored"]["risk_score"] >= threshold, "entity_id"]
    )
    actual_entities = set(register["entity_id"])
    register_columns = set(register.columns)
    model_only = register[register["rules_fired"].eq("NONE")]
    graph_keys_valid = set(graph["entity_id"]).issubset(actual_entities)
    allowed_due = {item["priority"]: int(item["due_hours"]) for item in policy["priority_bands"]}
    sla_valid = all(
        int(row.due_hours) == allowed_due[row.priority] for row in register.itertuples()
    )
    alerts_per_week = float(
        evidence["metrics"]["operating_point"]["entity_level"]["alerts_per_week"]
    )
    capacity = float(
        evidence["metrics"]["operating_point"]["entity_level"][
            "analyst_capacity_per_week"
        ]
    )
    return [
        {
            "gate_id": "QUEUE-01",
            "name": "One investigation per alerted entity",
            "status": "PASS" if actual_entities == expected_entities else "BLOCK",
            "observed": f"{len(actual_entities)} registered / {len(expected_entities)} expected",
        },
        {
            "gate_id": "LABEL-01",
            "name": "Ground truth quarantined from investigator workflow",
            "status": "PASS" if not (register_columns & GROUND_TRUTH_COLUMNS) else "BLOCK",
            "observed": "0 evaluation-label columns in the case register",
        },
        {
            "gate_id": "GRAPH-01",
            "name": "Entity graph referential integrity",
            "status": "PASS" if graph_keys_valid else "BLOCK",
            "observed": f"{len(graph)} entity-counterparty edges linked to registered cases",
        },
        {
            "gate_id": "REASON-01",
            "name": "Explainability boundary",
            "status": "PASS"
            if model_only["typology_hypothesis"].eq("UNCLASSIFIED-MODEL-ONLY").all()
            else "BLOCK",
            "observed": f"{len(model_only)} model-only cases remain explicitly unclassified",
        },
        {
            "gate_id": "SLA-01",
            "name": "Priority and review clock",
            "status": "PASS" if sla_valid else "BLOCK",
            "observed": "P0/P1/P2 cases carry 4/24/72-hour review clocks",
        },
        {
            "gate_id": "CAP-01",
            "name": "Illustrative analyst capacity",
            "status": "PASS" if alerts_per_week <= capacity else "REVIEW",
            "observed": f"{alerts_per_week:.1f} alerts/week against {capacity:.1f} capacity",
        },
        {
            "gate_id": "HUMAN-01",
            "name": "Human disposition approval",
            "status": "REVIEW",
            "observed": "0 authorized human dispositions recorded",
        },
        {
            "gate_id": "FILING-01",
            "name": "Regulatory reporting decision",
            "status": "REVIEW",
            "observed": "0 reporting recommendations approved or filed",
        },
    ]


def release_decision(gates: list[dict[str, str]]) -> str:
    statuses = {gate["status"] for gate in gates}
    if "BLOCK" in statuses:
        return "BLOCKED"
    if "REVIEW" in statuses:
        return "REVIEW REQUIRED"
    return "READY"


def build_release(
    evidence: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = evidence or load_evidence()
    policy = policy or load_policy()
    register = build_case_register(evidence, policy)
    graph = build_entity_graph(register, evidence)
    catalogue = typology_catalogue(policy)
    gates = release_gates(register, graph, evidence, policy)
    required = [SCORED_PATH, ENTITIES_PATH, COUNTERPARTIES_PATH, CASES_PATH, METRICS_PATH]
    fingerprint_material = register.to_csv(index=False, lineterminator="\n") + graph.to_csv(
        index=False, lineterminator="\n"
    )
    fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()
    summary = {
        "schema_version": 1,
        "control_id": policy["control_id"],
        "policy_version": policy["policy_version"],
        "decision": release_decision(gates),
        "investigations": int(len(register)),
        "p0_investigations": int(register["priority"].eq("P0").sum()),
        "p1_investigations": int(register["priority"].eq("P1").sum()),
        "p2_investigations": int(register["priority"].eq("P2").sum()),
        "graph_edges": int(len(graph)),
        "typologies": int(len(catalogue)),
        "gates_passed": sum(gate["status"] == "PASS" for gate in gates),
        "gates_review": sum(gate["status"] == "REVIEW" for gate in gates),
        "gates_blocked": sum(gate["status"] == "BLOCK" for gate in gates),
        "human_dispositions_recorded": 0,
        "reporting_filings_recorded": 0,
        "register_fingerprint": fingerprint,
        "human_decision_boundary": policy["human_decision_boundary"],
        "demonstration_boundary": (
            "Synthetic portfolio workflow only; not a compliance product, legal conclusion, "
            "customer decision, suspicious transaction report, or regulatory filing."
        ),
    }
    manifest = {
        "schema_version": 1,
        "control_id": policy["control_id"],
        "decision": summary["decision"],
        "register_fingerprint": fingerprint,
        "policy_sha256": canonical_sha256(POLICY_PATH),
        "input_sha256": {
            path.relative_to(ROOT).as_posix(): (
                decision_metrics_sha256(path)
                if path == METRICS_PATH
                else canonical_sha256(path)
            )
            for path in required
        },
        "metrics_hash_scope": (
            "Only the operating threshold, entity alerts, alerts per week and analyst "
            "capacity consumed by AML-INV-01 are included in the metrics digest."
        ),
        "ground_truth_use": (
            "Evaluation labels remain in pipeline evidence and are excluded from workflow "
            "state, priority, hypothesis, next-step and disposition fields."
        ),
        "data_classification": policy["data_classification"],
        "human_decision_boundary": policy["human_decision_boundary"],
    }
    return {
        "summary": summary,
        "register": register,
        "graph": graph,
        "catalogue": catalogue,
        "gates": gates,
        "manifest": manifest,
    }


def reverification_probe(
    release: dict[str, Any], evidence: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    changed = deepcopy(release)
    changed["register"] = release["register"].iloc[1:].copy()
    after_gates = release_gates(changed["register"], changed["graph"], evidence, policy)
    return {
        "probe": "remove one investigation from the register in memory",
        "decision_before": release["summary"]["decision"],
        "decision_after": release_decision(after_gates),
        "queue_gate_before": next(
            g["status"] for g in release["gates"] if g["gate_id"] == "QUEUE-01"
        ),
        "queue_gate_after": next(g["status"] for g in after_gates if g["gate_id"] == "QUEUE-01"),
        "source_data_mutated": False,
        "boundary": (
            "The failure drill changes an in-memory copy only; committed evidence "
            "is unchanged."
        ),
    }


def decision_packet(release: dict[str, Any], probe: dict[str, Any]) -> str:
    summary = release["summary"]
    lines = [
        "# AML-INV-01 Investigator Decision Packet",
        "",
        (
            "> Synthetic portfolio evidence. This is not a compliance product, legal "
            "conclusion, customer decision, suspicious transaction report, or regulatory "
            "filing."
        ),
        "",
        "## Release decision",
        "",
        (
            f"**{summary['decision']}** — {summary['gates_passed']} gates pass, "
            f"{summary['gates_review']} require review, and "
            f"{summary['gates_blocked']} block."
        ),
        "",
        (
            f"The register contains **{summary['investigations']} investigations** "
            f"({summary['p0_investigations']} P0, {summary['p1_investigations']} P1, "
            f"{summary['p2_investigations']} P2), **{summary['graph_edges']} graph edges** "
            f"and **{summary['typologies']} governed typology hypotheses**."
        ),
        "",
        "## Gate docket",
        "",
        "| Gate | Status | Observed |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {gate['gate_id']} · {gate['name']} | **{gate['status']}** | {gate['observed']} |"
        for gate in release["gates"]
    )
    lines.extend(
        [
            "",
            "## Human decision boundary",
            "",
            release["summary"]["human_decision_boundary"],
            "",
            (
                "The public workflow records zero human dispositions and zero regulatory "
                "filings. Risk scores and rules prioritize evidence; they do not determine "
                "suspicion or authorize action."
            ),
            "",
            "## Re-verification proof",
            "",
            (
                "Removing one case in memory moves QUEUE-01 from "
                f"**{probe['queue_gate_before']}** to **{probe['queue_gate_after']}** and "
                f"the release from **{probe['decision_before']}** to "
                f"**{probe['decision_after']}** without mutating a source file."
            ),
            "",
            "## Required disposition",
            "",
            "1. Validate evidence for each P0 case inside its review clock.",
            (
                "2. Record an authorized human disposition in the institution's "
                "controlled case-management system."
            ),
            "3. Keep regulatory reporting decisions outside this public demonstration.",
            (
                "4. Rebuild after any policy, threshold, feature, rule, model or "
                "source-evidence change."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> dict[str, Any]:
    policy = load_policy()
    evidence = load_evidence()
    release = build_release(evidence, policy)
    probe = reverification_probe(release, evidence, policy)
    RESULTS.mkdir(parents=True, exist_ok=True)
    release["register"].to_csv(REGISTER_PATH, index=False, lineterminator="\n")
    release["graph"].to_csv(GRAPH_PATH, index=False, lineterminator="\n")
    release["catalogue"].to_csv(TYPOLOGY_PATH, index=False, lineterminator="\n")
    pd.DataFrame(release["gates"]).to_csv(GATES_PATH, index=False, lineterminator="\n")
    for path, payload in (
        (SUMMARY_PATH, release["summary"]),
        (MANIFEST_PATH, release["manifest"]),
        (PROBE_PATH, probe),
    ):
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    PACKET_PATH.write_text(decision_packet(release, probe), encoding="utf-8", newline="\n")
    print(
        f"{policy['control_id']} {release['summary']['decision']} · "
        f"{release['summary']['gates_passed']} pass / "
        f"{release['summary']['gates_review']} review / "
        f"{release['summary']['gates_blocked']} block"
    )
    return release


if __name__ == "__main__":
    main()
