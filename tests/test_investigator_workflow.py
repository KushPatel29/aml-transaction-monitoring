"""Contracts for the governed investigator workflow and its fail-closed release."""

from __future__ import annotations

import json
from copy import deepcopy

import pandas as pd
import pytest

from governance import investigator_workflow as iw


@pytest.fixture(scope="module")
def policy():
    return iw.load_policy()


@pytest.fixture(scope="module")
def evidence():
    return iw.load_evidence()


@pytest.fixture(scope="module")
def release(evidence, policy):
    return iw.build_release(evidence, policy)


def test_policy_is_versioned(policy):
    assert policy["schema_version"] == 1
    assert policy["control_id"] == "AML-INV-01"
    assert policy["policy_version"] == "1.0.0"


def test_policy_governs_five_distinct_typologies(policy):
    typologies = policy["typologies"]
    assert len(typologies) == 5
    assert len({item["typology_id"] for item in typologies}) == 5
    assert len({item["rule_code"] for item in typologies}) == 5


def test_every_typology_names_a_lawful_lookalike_and_discriminator(policy):
    for item in policy["typologies"]:
        assert item["lawful_lookalike"]
        assert item["discriminator"]
        assert len(item["minimum_evidence"]) >= 4
        assert item["known_limitation"]


def test_priority_bands_have_strict_review_clocks(policy):
    observed = {item["priority"]: item["due_hours"] for item in policy["priority_bands"]}
    assert observed == {"P0": 4, "P1": 24, "P2": 72}


def test_register_has_one_case_per_alerted_entity(release, evidence):
    threshold = evidence["metrics"]["operating_point"]["threshold"]
    expected = set(
        evidence["scored"].loc[
            evidence["scored"]["risk_score"] >= threshold, "entity_id"
        ]
    )
    register = release["register"]
    assert register["investigation_id"].is_unique
    assert set(register["entity_id"]) == expected
    assert len(register) == evidence["metrics"]["operating_point"]["entity_level"]["alerts"]


def test_ground_truth_is_not_exposed_in_the_register(release):
    assert not (set(release["register"].columns) & iw.GROUND_TRUTH_COLUMNS)


def test_ground_truth_cannot_change_priority_or_workflow_state(evidence, policy):
    baseline = iw.build_case_register(evidence, policy)
    changed = deepcopy(evidence)
    changed["scored"] = evidence["scored"].copy()
    changed["scored"]["is_suspicious"] = 1 - changed["scored"]["is_suspicious"]
    changed["scored"]["typology"] = "fabricated-label"
    changed["scored"]["typology_variant"] = "fabricated-variant"
    changed["scored"]["case_id"] = "FABRICATED-CASE"
    after = iw.build_case_register(changed, policy)
    columns = [
        "investigation_id",
        "priority",
        "case_state",
        "typology_hypothesis",
        "recommended_next_step",
        "decision_status",
    ]
    pd.testing.assert_frame_equal(baseline[columns], after[columns])


def test_every_case_preserves_the_human_decision_boundary(release):
    register = release["register"]
    assert register["decision_status"].eq("HUMAN DECISION REQUIRED").all()
    assert register["filing_status"].eq("NOT ASSESSED OR FILED").all()


def test_case_states_are_policy_controlled(release, policy):
    assert set(release["register"]["case_state"]) <= set(policy["allowed_states"])


def test_priority_clocks_match_policy(release, policy):
    due = {item["priority"]: item["due_hours"] for item in policy["priority_bands"]}
    for row in release["register"].itertuples():
        assert row.due_hours == due[row.priority]


def test_model_only_cases_remain_unclassified(release):
    model_only = release["register"].loc[release["register"]["rules_fired"].eq("NONE")]
    assert not model_only.empty
    assert model_only["typology_hypothesis"].eq("UNCLASSIFIED-MODEL-ONLY").all()
    assert model_only["evidence_references"].str.contains("no rule reason").all()


def test_graph_edges_reference_registered_cases(release):
    assert set(release["graph"]["entity_id"]) <= set(release["register"]["entity_id"])
    assert release["graph"][["entity_id", "counterparty_id"]].duplicated().sum() == 0


def test_graph_aggregates_real_transaction_evidence(release):
    graph = release["graph"]
    assert (graph["transactions"] > 0).all()
    assert (graph["amount_cad"] > 0).all()
    assert (graph["shared_entity_degree"] >= 1).all()
    assert graph["first_seen"].le(graph["last_seen"]).all()


def test_typology_catalogue_is_machine_readable(release):
    catalogue = release["catalogue"]
    assert len(catalogue) == 5
    assert catalogue["minimum_evidence"].str.contains(";").all()


def test_release_gate_ids_are_unique(release):
    ids = [gate["gate_id"] for gate in release["gates"]]
    assert len(ids) == len(set(ids)) == 8


def test_technical_release_gates_pass(release):
    gates = {gate["gate_id"]: gate for gate in release["gates"]}
    for gate_id in ("QUEUE-01", "LABEL-01", "GRAPH-01", "REASON-01", "SLA-01", "CAP-01"):
        assert gates[gate_id]["status"] == "PASS"


def test_human_and_filing_gates_remain_review(release):
    gates = {gate["gate_id"]: gate for gate in release["gates"]}
    assert gates["HUMAN-01"]["status"] == "REVIEW"
    assert gates["FILING-01"]["status"] == "REVIEW"


def test_release_is_review_required_not_approved(release):
    summary = release["summary"]
    assert summary["decision"] == "REVIEW REQUIRED"
    assert summary["gates_passed"] == 6
    assert summary["gates_review"] == 2
    assert summary["gates_blocked"] == 0
    assert summary["human_dispositions_recorded"] == 0
    assert summary["reporting_filings_recorded"] == 0


def test_register_fingerprint_is_deterministic(evidence, policy):
    first = iw.build_release(evidence, policy)["summary"]["register_fingerprint"]
    second = iw.build_release(evidence, policy)["summary"]["register_fingerprint"]
    assert first == second
    assert len(first) == 64


def test_manifest_hashes_every_required_input(release):
    manifest = release["manifest"]
    assert set(manifest["input_sha256"]) == {
        "results/scored_transactions.csv.gz",
        "data/generated/entities.csv.gz",
        "data/generated/counterparties.csv.gz",
        "data/generated/cases.csv.gz",
        "results/metrics.json",
    }
    assert all(len(value) == 64 for value in manifest["input_sha256"].values())


def test_hash_is_cross_platform_newline_stable(tmp_path):
    lf = tmp_path / "lf.csv"
    crlf = tmp_path / "crlf.csv"
    lf.write_bytes(b"case,status\n1,open\n")
    crlf.write_bytes(b"case,status\r\n1,open\r\n")
    assert iw.canonical_sha256(lf) == iw.canonical_sha256(crlf)


def test_metrics_hash_excludes_volatile_runtime_metadata(tmp_path):
    baseline = json.loads(iw.METRICS_PATH.read_text(encoding="utf-8"))
    changed = deepcopy(baseline)
    changed["generated_at_utc"] = "2099-01-01T00:00:00+00:00"
    changed["python_version"] = "99.0"
    changed["runtime_seconds"] = {"total": 999.0}
    for model in changed["model_bakeoff"]:
        model["fit_score_seconds"] = 999.0
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(baseline), encoding="utf-8")
    second.write_text(json.dumps(changed), encoding="utf-8")
    assert iw.decision_metrics_sha256(first) == iw.decision_metrics_sha256(second)


def test_metrics_hash_changes_when_decision_evidence_changes(tmp_path):
    baseline = json.loads(iw.METRICS_PATH.read_text(encoding="utf-8"))
    changed = deepcopy(baseline)
    changed["operating_point"]["threshold"] += 0.1
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(baseline), encoding="utf-8")
    second.write_text(json.dumps(changed), encoding="utf-8")
    assert iw.decision_metrics_sha256(first) != iw.decision_metrics_sha256(second)


def test_reverification_probe_fails_closed(release, evidence, policy):
    probe = iw.reverification_probe(release, evidence, policy)
    assert probe["queue_gate_before"] == "PASS"
    assert probe["queue_gate_after"] == "BLOCK"
    assert probe["decision_before"] == "REVIEW REQUIRED"
    assert probe["decision_after"] == "BLOCKED"
    assert probe["source_data_mutated"] is False


def test_decision_packet_preserves_non_decisional_boundary(release, evidence, policy):
    probe = iw.reverification_probe(release, evidence, policy)
    packet = iw.decision_packet(release, probe)
    assert "**REVIEW REQUIRED**" in packet
    assert "zero human dispositions" in packet
    assert "zero regulatory filings" in packet
    assert "Risk scores and rules prioritize evidence" in packet


def test_published_outputs_match_current_evidence(release):
    published = json.loads(iw.SUMMARY_PATH.read_text(encoding="utf-8"))
    assert published == release["summary"]
    register = pd.read_csv(iw.REGISTER_PATH, keep_default_na=False)
    graph = pd.read_csv(iw.GRAPH_PATH, keep_default_na=False)
    pd.testing.assert_frame_equal(register, release["register"], check_dtype=False)
    pd.testing.assert_frame_equal(graph, release["graph"], check_dtype=False)


def test_unknown_policy_schema_fails_closed(tmp_path):
    policy = json.loads(iw.POLICY_PATH.read_text(encoding="utf-8"))
    policy["schema_version"] = 99
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        iw.load_policy(path)
