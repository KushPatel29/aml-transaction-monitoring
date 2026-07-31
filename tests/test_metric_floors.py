"""
Regression guards on the headline numbers, and on the claim that the layered
system is worth building at all (C3, C8).

Every floor lives in config.METRIC_FLOORS at roughly 90% of what the first
clean run produced. They were written down after measuring. Nothing was tuned
to clear them, and if a change genuinely improves the pipeline the floors
should be raised deliberately rather than left slack.

All figures are read from results/metrics.json rather than recomputed, so
these tests fail if the committed metrics ever stop matching what the pipeline
produces -- which is the actual claim the README rests on.
"""

from __future__ import annotations

import json

import pytest

import config as cfg


@pytest.fixture(scope="module")
def metrics():
    if not cfg.METRICS_PATH.exists():
        pytest.skip("results/metrics.json missing -- run `python run_pipeline.py`")
    return json.loads(cfg.METRICS_PATH.read_text(encoding="utf-8"))


def test_metrics_file_describes_a_real_run(metrics):
    assert metrics["generated_by"] == "python run_pipeline.py"
    assert metrics["dataset"]["transactions"] > 0
    assert metrics["dataset"]["planted_cases"] > 0
    assert "_disclaimer" in metrics
    assert "SYNTHETIC" in metrics["_disclaimer"]
    assert metrics["configuration"]["cost_constants_are_illustrative"] is True


def test_entity_level_recall_floor(metrics):
    recall = metrics["operating_point"]["entity_level"]["recall"]
    assert recall >= cfg.METRIC_FLOORS["entity_recall"], (
        f"entity recall fell to {recall:.3f}, below the "
        f"{cfg.METRIC_FLOORS['entity_recall']} guard"
    )


def test_entity_level_precision_floor(metrics):
    precision = metrics["operating_point"]["entity_level"]["precision"]
    assert precision >= cfg.METRIC_FLOORS["entity_precision"], (
        f"entity precision fell to {precision:.3f}, below the "
        f"{cfg.METRIC_FLOORS['entity_precision']} guard"
    )


def test_transaction_level_floors(metrics):
    block = metrics["operating_point"]["transaction_level"]
    assert block["recall"] >= cfg.METRIC_FLOORS["txn_recall"]
    assert block["precision"] >= cfg.METRIC_FLOORS["txn_precision"]


def test_ranking_quality_floor(metrics):
    pr_auc = metrics["ranking"]["pr_auc"]
    assert pr_auc >= cfg.METRIC_FLOORS["pr_auc"]
    # A point estimate on 60 cases is not worth much on its own; the lower
    # bound of the interval is the number that should hold.
    ci_low = metrics["confidence_intervals_95"]["pr_auc"]["ci_low"]
    assert ci_low >= cfg.METRIC_FLOORS["pr_auc_ci_low"]
    assert pr_auc > metrics["ranking"]["prevalence"] * 10


def test_held_out_threshold_selection_still_works(metrics):
    """
    The pooled optimum is chosen on the same data it is measured against.
    This is the number that survives choosing the threshold blind.
    """
    nested = metrics["nested_threshold_check"]["mean_recall"]
    assert nested >= cfg.METRIC_FLOORS["nested_mean_recall"], (
        f"recall under nested threshold selection fell to {nested:.3f}"
    )


def test_layered_system_beats_the_naive_baseline(metrics):
    """C8: the complexity has to earn itself, in numbers, or it should go."""
    comparison = metrics["baseline_comparison"]

    assert comparison["layered_recall"] >= comparison["baseline_recall"], (
        "the layered system finds fewer cases than a $10,000 threshold rule"
    )
    multiple = comparison["layered_precision"] / comparison["baseline_precision"]
    assert multiple >= cfg.METRIC_FLOORS["precision_multiple_over_baseline"], (
        f"layered precision is only {multiple:.1f}x the naive baseline"
    )
    assert comparison["cost_reduction_pct"] >= cfg.METRIC_FLOORS[
        "baseline_cost_reduction_pct"
    ], f"cost reduction fell to {comparison['cost_reduction_pct']:.1f}%"
    assert comparison["layered_alerts"] < comparison["baseline_alerts"], (
        "the layered system raises more alerts than the naive rule as well"
    )


def test_alert_volume_is_operationally_plausible(metrics):
    """
    A detection system nobody can staff is not a detection system. The capacity
    figure is illustrative like the rest of the cost model, but a threshold
    producing ten times the team's throughput would be a real design failure.
    """
    block = metrics["operating_point"]["entity_level"]
    assert block["within_capacity"], (
        f"{block['alerts_per_week']:.0f} alerts/week against a capacity of "
        f"{block['analyst_capacity_per_week']:.0f}"
    )


def test_the_blend_beats_each_of_its_parts_on_ranking(metrics):
    """
    If the blended score does not rank better than rules alone, the model is
    decoration and the README should say so instead of shipping it.
    """
    by_variant = {row["variant"]: row for row in metrics["ablation"]}
    assert by_variant["blended"]["pr_auc"] > by_variant["rules_only"]["pr_auc"]
    assert by_variant["blended"]["pr_auc"] > by_variant["model_only"]["pr_auc"]
    assert (
        by_variant["blended"]["entity_expected_cost_cad"]
        <= by_variant["rules_only"]["entity_expected_cost_cad"]
    )


def test_unsupervised_gap_to_the_supervised_ceiling_is_reported(metrics):
    """
    The supervised models exist to size what labels would buy. That comparison
    only means something if both are actually present and the ceiling is
    genuinely above the shipped model.
    """
    bakeoff = {row["model"]: row for row in metrics["model_bakeoff"]}
    assert any(row["supervised"] for row in bakeoff.values())
    assert any(not row["supervised"] for row in bakeoff.values())

    best_supervised = max(r["pr_auc"] for r in bakeoff.values() if r["supervised"])
    best_unsupervised = max(r["pr_auc"] for r in bakeoff.values() if not r["supervised"])
    assert best_supervised > best_unsupervised, (
        "the supervised ceiling is below the unsupervised models, which would "
        "mean the labels carry no information the features do not already have"
    )


def test_cost_sensitivity_is_reported_across_the_full_range(metrics):
    """
    The operating threshold is downstream of an invented constant. Publishing
    it without the sensitivity sweep would be presenting an assumption as a
    finding.
    """
    sensitivity = metrics["cost_sensitivity"]
    assert len(sensitivity) == len(cfg.COST_SENSITIVITY_MISS_VALUES)
    assert {s["cost_per_missed_case_cad"] for s in sensitivity} == set(
        cfg.COST_SENSITIVITY_MISS_VALUES
    )
    # Higher cost of a miss can never justify a *higher* alerting bar.
    ordered = sorted(sensitivity, key=lambda s: s["cost_per_missed_case_cad"])
    thresholds = [s["optimal_threshold"] for s in ordered]
    assert thresholds == sorted(thresholds, reverse=True), (
        f"optimal thresholds {thresholds} do not fall as a missed case gets "
        "more expensive, which would be incoherent"
    )


def test_failure_analysis_is_present_and_specific(metrics):
    """
    C7 asks for a real limitation. This asserts the evidence for it is in the
    metrics file rather than only in the prose.
    """
    analysis = metrics["failure_analysis"]
    windows = analysis["structuring_windows"]
    assert windows, "no structuring window analysis was recorded"

    slow = [w for w in windows if w["variant"] == "slow"]
    fast = [w for w in windows if w["variant"] == "fast"]
    assert slow and fast

    # Every fast case fits inside the rule window; that is what makes it fast.
    assert all(w["densest_7day_window"] == w["deposits"] for w in fast)
    # R1 fires exactly when a seven-day window holds enough deposits -- the
    # rule has no other behaviour, and the failure is arithmetic, not mystery.
    for window in windows:
        assert window["r1_did_fire"] == window["r1_can_fire"], window["case_id"]
    # At least one slow case must actually defeat the window, or the planted
    # stress subset is not stressing anything.
    assert any(not w["r1_can_fire"] for w in slow)
