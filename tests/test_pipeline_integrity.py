"""
Structural guarantees the rest of the suite assumes: that ground truth never
reaches the models, that the cross-validation is genuinely entity-disjoint,
that the fast vectorised sweep agrees with the obvious slow implementation,
and that the pipeline is reproducible.

These are the tests that catch the failures nobody notices, because a leaking
label or a fold boundary in the wrong place makes every other number *better*.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

import config as cfg
from src import anomaly_model, cost_model, risk_scorer


# ---------------------------------------------------------------------------
# Label leakage
# ---------------------------------------------------------------------------
def test_ground_truth_never_reaches_the_model_matrix():
    overlap = set(cfg.MODEL_FEATURE_COLUMNS) & set(cfg.GROUND_TRUTH_COLUMNS)
    assert not overlap, f"ground-truth columns in the model matrix: {sorted(overlap)}"


def test_model_features_all_come_from_the_sql_layer():
    """
    Every model input must exist in the parity-tested feature set (or be the
    raw amount, which comes straight off the source table). An input invented
    in Python would sit outside the C2 contract entirely.
    """
    allowed = set(cfg.PARITY_COLUMNS) | {"amount_cents"}
    unknown = set(cfg.MODEL_FEATURE_COLUMNS) - allowed
    assert not unknown, f"model features with no SQL definition: {sorted(unknown)}"


def test_rule_support_columns_are_not_model_features():
    overlap = set(cfg.MODEL_FEATURE_COLUMNS) & set(cfg.RULE_SUPPORT_COLUMNS)
    assert not overlap, (
        f"{sorted(overlap)} are raw entity history, not behavioural signal, and "
        "should not be model inputs"
    )


def test_scored_output_carries_no_hidden_label_columns(scored):
    """
    The dashboard reads the scored artefact. Ground truth is present in it on
    purpose -- the app shows the evaluation view -- but it must not be sitting
    inside the feature block where a future change could sweep it into a model.
    """
    feature_block = set(cfg.MODEL_FEATURE_COLUMNS)
    assert not feature_block & set(cfg.GROUND_TRUTH_COLUMNS)
    for column in cfg.GROUND_TRUTH_COLUMNS:
        assert column in scored.columns, f"{column} missing from the scored output"


# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------
def test_folds_are_entity_disjoint(scored):
    """
    An entity in two folds means its own history trained the model that scored
    it. Features here are entity-relative, so that leak would inflate every
    downstream number silently.
    """
    per_entity = scored.groupby("entity_id")["fold"].nunique()
    offenders = per_entity[per_entity > 1]
    assert offenders.empty, (
        f"{len(offenders)} entities appear in more than one fold, e.g. "
        f"{offenders.index[0]}"
    )


def test_folds_are_balanced_on_cases(scored):
    cases_per_fold = (
        scored[scored["is_suspicious"] == 1].groupby("fold")["case_id"].nunique()
    )
    assert len(cases_per_fold) == cfg.N_CV_FOLDS
    assert cases_per_fold.max() - cases_per_fold.min() <= 1, (
        f"case counts per fold are {cases_per_fold.to_dict()}, which would make "
        "the per-fold metrics noise"
    )


def test_fold_assignment_is_deterministic():
    entities = pd.Series([f"ENT-{i:05d}" for i in range(500)])
    has_case = pd.Series([i % 25 == 0 for i in range(500)])
    first = anomaly_model.assign_entity_folds(entities, has_case)
    second = anomaly_model.assign_entity_folds(entities, has_case)
    assert first.equals(second)


def test_percentile_normalisation_uses_the_training_distribution():
    """
    Normalising test scores among themselves would force a fixed fraction of
    every batch to look anomalous no matter how quiet the batch was.
    """
    train = np.arange(100, dtype=float)
    quiet_batch = np.array([1.0, 2.0, 3.0])
    result = anomaly_model._percentile_against(train, quiet_batch)
    assert result.max() < 0.10, (
        "a batch well below the training distribution still produced high "
        "percentiles, so the normaliser is self-referential"
    )


# ---------------------------------------------------------------------------
# The fast sweep must agree with the obvious implementation
# ---------------------------------------------------------------------------
def test_vectorised_sweep_matches_the_groupby_implementation(scored):
    """
    The sweep was rewritten to sort once and read cumulative counts so a
    1,001-point grid stays affordable. That optimisation is only safe if it
    still produces exactly what the straightforward per-threshold groupby did.
    """
    risk = scored["risk_score"].to_numpy()
    y_true = scored["is_suspicious"].to_numpy()
    entity_ids = scored["entity_id"].to_numpy()

    probes = [0.0, 10.0, 39.6, 39.7, 50.0, 67.9, 90.0, 100.0]
    fast = risk_scorer.sweep(risk, y_true, entity_ids, thresholds=probes)

    for threshold in probes:
        slow = cost_model.evaluate(y_true, risk >= threshold, entity_ids)
        row = fast.loc[fast["threshold"] == threshold].iloc[0]
        for level, block in (("txn", "transaction_level"), ("entity", "entity_level")):
            for key in ("true_positives", "false_positives", "false_negatives", "alerts"):
                assert row[f"{level}_{key}"] == slow[block][key], (
                    f"{level}.{key} disagrees at threshold {threshold}"
                )
            assert row[f"{level}_precision"] == pytest.approx(slow[block]["precision"])
            assert row[f"{level}_recall"] == pytest.approx(slow[block]["recall"])


def test_sweep_is_monotone_in_the_right_directions(scored):
    risk = scored["risk_score"].to_numpy()
    frame = risk_scorer.sweep(
        risk, scored["is_suspicious"].to_numpy(), scored["entity_id"].to_numpy()
    )
    assert (frame["entity_alerts"].diff().dropna() <= 0).all(), (
        "raising the threshold produced more alerts"
    )
    assert (frame["entity_recall"].diff().dropna() <= 1e-12).all(), (
        "raising the threshold improved recall"
    )
    assert frame["entity_recall"].iloc[0] == 1.0
    assert frame["entity_alerts"].iloc[-1] <= frame["entity_alerts"].iloc[0]


def test_entity_consolidation_reduces_alert_counts(scored):
    """
    The correction the brief needed: an entity with six flagged rows is one
    investigation, not six.
    """
    flagged = scored["risk_score"].to_numpy() >= 39.6
    evaluated = cost_model.evaluate(
        scored["is_suspicious"].to_numpy(), flagged, scored["entity_id"].to_numpy()
    )
    assert (
        evaluated["entity_level"]["alerts"] < evaluated["transaction_level"]["alerts"]
    )
    assert (
        evaluated["entity_level"]["expected_cost_cad"]
        < evaluated["transaction_level"]["expected_cost_cad"]
    )


# ---------------------------------------------------------------------------
# Cost arithmetic
# ---------------------------------------------------------------------------
def test_expected_cost_is_the_stated_formula():
    matrix = cost_model.Confusion(
        true_positives=10, false_positives=4, false_negatives=2, true_negatives=100
    )
    assert cost_model.expected_cost(matrix, 200.0, 10_000.0) == 4 * 200.0 + 2 * 10_000.0
    assert matrix.precision == pytest.approx(10 / 14)
    assert matrix.recall == pytest.approx(10 / 12)
    assert matrix.alerts == 14


def test_cost_constants_are_flagged_illustrative_in_config():
    source = (cfg.ROOT / "config.py").read_text(encoding="utf-8")
    block = source[source.index("ILLUSTRATIVE COST MODEL"):]
    # Token checks rather than an exact phrase -- the comment is wrapped, so a
    # phrase match would break on reflow rather than on the guarantee going away.
    assert "INVENTED" in block
    for token in ("industry", "benchmarks", "regulatory", "illustrative"):
        assert token in block.lower(), f"the cost block no longer says {token!r}"
    assert cfg.COST_PER_INVESTIGATION_CAD == (
        cfg.ANALYST_HOURLY_RATE_CAD * cfg.INVESTIGATION_HOURS_PER_ALERT
    )


def test_naive_baseline_is_exactly_the_stated_rule(scored):
    flags = cost_model.naive_baseline_flags(scored["amount_cad"].to_numpy())
    assert flags.sum() == (scored["amount_cad"] > cfg.NAIVE_BASELINE_THRESHOLD_CAD).sum()
    assert not flags[scored["amount_cad"] <= cfg.NAIVE_BASELINE_THRESHOLD_CAD].any()


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------
def _content_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(
        pd.util.hash_pandas_object(frame, index=False).values.tobytes()
    ).hexdigest()


@pytest.mark.slow
def test_generator_is_reproducible(transactions, tmp_path, monkeypatch):
    """
    Regenerating from the same seed must reproduce the committed data exactly.
    Content-hashed rather than byte-compared so the check is about the data
    rather than about gzip framing.
    """
    import sys

    sys.path.insert(0, str(cfg.ROOT / "data"))
    import generate_transactions

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    for name in ("ENTITIES_PATH", "COUNTERPARTIES_PATH", "TRANSACTIONS_PATH", "CASES_PATH"):
        monkeypatch.setattr(cfg, name, tmp_path / getattr(cfg, name).name)

    generate_transactions.main()
    regenerated = pd.read_csv(cfg.TRANSACTIONS_PATH, keep_default_na=False)

    assert len(regenerated) == len(transactions)
    assert _content_hash(regenerated) == _content_hash(transactions), (
        "regenerating from the same seed produced different data"
    )


def test_metrics_json_matches_the_committed_scored_output(scored):
    """
    The README quotes metrics.json. This asserts metrics.json still describes
    the artefact actually sitting in the repo, which is the only thing that
    makes C3 mean anything.
    """
    if not cfg.METRICS_PATH.exists():
        pytest.skip("metrics.json missing")
    metrics = json.loads(cfg.METRICS_PATH.read_text(encoding="utf-8"))

    assert metrics["dataset"]["transactions"] == len(scored)
    assert metrics["dataset"]["suspicious_transactions"] == int(scored["is_suspicious"].sum())
    assert metrics["dataset"]["entities_with_a_case"] == int(
        scored.loc[scored["is_suspicious"] == 1, "entity_id"].nunique()
    )

    threshold = metrics["operating_point"]["threshold"]
    recomputed = cost_model.evaluate(
        scored["is_suspicious"].to_numpy(),
        scored["risk_score"].to_numpy() >= threshold,
        scored["entity_id"].to_numpy(),
    )
    for level in ("transaction_level", "entity_level"):
        for key in ("true_positives", "false_positives", "false_negatives"):
            assert metrics["operating_point"][level][key] == recomputed[level][key], (
                f"metrics.json {level}.{key} does not match the committed data"
            )
