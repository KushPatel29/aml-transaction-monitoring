"""
Unified 0-100 risk score, threshold sweep, and the honesty checks around them.

The score itself is deliberately simple -- a weighted blend of "how many rules
fired" and "how anomalous the model thinks this is", both normalised to [0, 1].
An analyst has to be able to explain an alert to someone who did not build it,
and a learned combiner would trade that away for a fraction of a point.

Three things here exist specifically to stop the numbers flattering themselves:

  * `sweep` reports transaction-level AND entity-level metrics at every
    threshold, because the cost of a false positive is per investigation, not
    per row.

  * `nested_threshold_check` picks the operating threshold on training folds
    and applies it to held-out ones. The pooled optimum is chosen using the
    same data it is then measured on, which is mildly optimistic; this
    quantifies by how much.

  * `bootstrap_metrics` resamples ENTITIES, not transactions. With 60 cases,
    resampling rows would treat six transactions from one case as six
    independent observations and produce confidence intervals several times
    too narrow.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)

import config as cfg  # noqa: E402
from src import cost_model


def compute_risk_scores(rule_hits: pd.DataFrame, model_scores: pd.DataFrame,
                        model_name: str = "isolation_forest") -> pd.DataFrame:
    """
    Blend rule hits and the model's anomaly percentile into 0-100.

    The rule component saturates at RULE_SATURATION simultaneous hits: two
    rules firing is decisively different from one, three is not meaningfully
    different from two, and without the cap a transaction that trips four
    overlapping rules would crowd out everything else in the queue.
    """
    column = f"{model_name}_score"
    assert column in model_scores.columns, f"no scores for model '{model_name}'"

    merged = rule_hits.merge(
        model_scores[["transaction_id", "fold", column]],
        on="transaction_id", how="inner", validate="1:1",
    )
    assert len(merged) == len(rule_hits), "score join dropped rows"

    rule_component = np.minimum(
        merged["n_rules_fired"].to_numpy() / cfg.RULE_SATURATION, 1.0
    )
    model_component = merged[column].to_numpy()

    merged["rule_component"] = rule_component
    merged["model_component"] = model_component
    merged["risk_score"] = 100.0 * (
        cfg.RULE_WEIGHT * rule_component + cfg.MODEL_WEIGHT * model_component
    )
    return merged


def _confusion_curve(scores: np.ndarray, labels: np.ndarray,
                     thresholds: np.ndarray) -> dict[str, np.ndarray]:
    """
    True/false positives and negatives at every threshold, in one pass.

    Rebuilding the confusion matrix per threshold with a groupby is fine for a
    101-point integer sweep and hopeless for a 1,001-point one. Sorting once
    and reading cumulative counts makes the fine grid essentially free, which
    is what lets the sweep resolve the cost cliffs it was previously stepping
    over.
    """
    n = len(scores)
    labels = labels.astype(bool)
    total_positive = int(labels.sum())

    descending = np.argsort(-scores, kind="stable")
    cumulative_positive = np.concatenate([[0], np.cumsum(labels[descending])])

    ascending = np.sort(scores)
    flagged = n - np.searchsorted(ascending, thresholds, side="left")

    true_positive = cumulative_positive[flagged]
    false_positive = flagged - true_positive
    false_negative = total_positive - true_positive
    return {
        "true_positives": true_positive,
        "false_positives": false_positive,
        "false_negatives": false_negative,
        "true_negatives": n - flagged - false_negative,
        "alerts": flagged,
    }


def _rates(block: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    alerts = block["alerts"]
    actual = block["true_positives"] + block["false_negatives"]
    precision = np.divide(
        block["true_positives"], alerts, out=np.zeros(len(alerts), float), where=alerts > 0
    )
    recall = np.divide(
        block["true_positives"], actual, out=np.zeros(len(alerts), float), where=actual > 0
    )
    denominator = precision + recall
    f1 = np.divide(
        2 * precision * recall, denominator,
        out=np.zeros(len(alerts), float), where=denominator > 0,
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def sweep(risk_scores: np.ndarray, y_true: np.ndarray, entity_ids: np.ndarray,
          thresholds: list[float] | None = None,
          cost_per_miss: float = cfg.PROXY_COST_PER_MISSED_CASE_CAD) -> pd.DataFrame:
    """Precision, recall and expected cost at every threshold, both levels."""
    grid = np.asarray(thresholds if thresholds is not None else cfg.THRESHOLD_SWEEP, float)

    txn = _confusion_curve(risk_scores, y_true, grid)
    txn_rates = _rates(txn)

    # Entity level: an entity alerts when its peak-scoring transaction does.
    rolled = (
        pd.DataFrame({"entity_id": entity_ids, "score": risk_scores,
                      "y": y_true.astype(bool)})
        .groupby("entity_id", sort=True)
        .agg(score=("score", "max"), y=("y", "max"))
    )
    entity = _confusion_curve(
        rolled["score"].to_numpy(), rolled["y"].to_numpy(), grid
    )
    entity_rates = _rates(entity)

    weeks = 52.0
    capacity = cost_model.analyst_capacity_alerts_per_week()

    frame = pd.DataFrame({"threshold": grid})
    for key, values in txn.items():
        frame[f"txn_{key}"] = values
    for key, values in txn_rates.items():
        frame[f"txn_{key}"] = values
    frame["txn_expected_cost_cad"] = (
        txn["false_positives"] * cfg.COST_PER_INVESTIGATION_CAD
        + txn["false_negatives"] * cost_per_miss
    )

    for key, values in entity.items():
        frame[f"entity_{key}"] = values
    for key, values in entity_rates.items():
        frame[f"entity_{key}"] = values
    frame["entity_expected_cost_cad"] = (
        entity["false_positives"] * cfg.COST_PER_INVESTIGATION_CAD
        + entity["false_negatives"] * cost_per_miss
    )
    frame["entity_alerts_per_week"] = entity["alerts"] / weeks
    frame["entity_within_capacity"] = frame["entity_alerts_per_week"] <= capacity
    return frame


def cost_minimising_threshold(sweep_frame: pd.DataFrame, level: str = "entity") -> float:
    """
    Lowest-cost threshold, ties broken toward the HIGHER threshold.

    Ties are common on the flat stretches of the cost curve, and among equal-cost
    options the one raising fewer alerts is the one an analyst team would rather
    be handed.
    """
    column = f"{level}_expected_cost_cad"
    cheapest = sweep_frame[column].min()
    return float(sweep_frame.loc[sweep_frame[column] == cheapest, "threshold"].max())


def nested_threshold_check(risk_scores: np.ndarray, y_true: np.ndarray,
                           entity_ids: np.ndarray, folds: np.ndarray,
                           level: str = "entity") -> dict:
    """
    Choose the threshold on the training folds, measure it on the held-out one.

    The difference between this and the pooled-optimal result is the size of
    the optimism in quoting a threshold that was selected on the same data it
    is reported against. Reporting the gap is cheaper than pretending it is
    zero.
    """
    per_fold = []
    for fold in np.unique(folds):
        is_test = folds == fold
        train_sweep = sweep(
            risk_scores[~is_test], y_true[~is_test], entity_ids[~is_test]
        )
        chosen = cost_minimising_threshold(train_sweep, level=level)

        flagged = risk_scores[is_test] >= chosen
        evaluated = cost_model.evaluate(y_true[is_test], flagged, entity_ids[is_test])
        block = evaluated[f"{level}_level"]
        per_fold.append(
            {
                "fold": int(fold),
                "threshold_chosen_on_train": chosen,
                "precision": block["precision"],
                "recall": block["recall"],
                "expected_cost_cad": block["expected_cost_cad"],
            }
        )

    frame = pd.DataFrame(per_fold)
    return {
        "per_fold": per_fold,
        "mean_threshold": float(frame["threshold_chosen_on_train"].mean()),
        "mean_precision": float(frame["precision"].mean()),
        "mean_recall": float(frame["recall"].mean()),
        "total_expected_cost_cad": float(frame["expected_cost_cad"].sum()),
    }


def bootstrap_metrics(risk_scores: np.ndarray, y_true: np.ndarray,
                      entity_ids: np.ndarray, threshold: float,
                      iterations: int = cfg.BOOTSTRAP_ITERATIONS,
                      seed: int = cfg.BOOTSTRAP_SEED) -> dict:
    """
    95% intervals by resampling entities with replacement.

    Entities, not transactions. A case contributes several correlated rows, and
    resampling rows would count them as independent evidence and shrink every
    interval to something indefensible.
    """
    rng = np.random.default_rng(seed)
    unique_entities, entity_index = np.unique(entity_ids, return_inverse=True)
    rows_by_entity = [np.flatnonzero(entity_index == i) for i in range(len(unique_entities))]

    flagged = risk_scores >= threshold
    y_bool = y_true.astype(bool)

    # Per-entity confusion contributions at this threshold, so the transaction
    # level resamples with three lookups instead of a rescan.
    tp = np.array([int((flagged[r] & y_bool[r]).sum()) for r in rows_by_entity])
    fp = np.array([int((flagged[r] & ~y_bool[r]).sum()) for r in rows_by_entity])
    fn = np.array([int((~flagged[r] & y_bool[r]).sum()) for r in rows_by_entity])
    entity_is_case = np.array([bool(y_bool[r].any()) for r in rows_by_entity])
    entity_alerted = np.array([bool(flagged[r].any()) for r in rows_by_entity])

    samples = {
        "txn_precision": [], "txn_recall": [],
        "entity_precision": [], "entity_recall": [],
        "pr_auc": [],
    }
    n_entities = len(unique_entities)

    for _ in range(iterations):
        picked = rng.integers(0, n_entities, size=n_entities)

        tp_s, fp_s, fn_s = tp[picked].sum(), fp[picked].sum(), fn[picked].sum()
        samples["txn_precision"].append(tp_s / (tp_s + fp_s) if (tp_s + fp_s) else 0.0)
        samples["txn_recall"].append(tp_s / (tp_s + fn_s) if (tp_s + fn_s) else 0.0)

        case_s = entity_is_case[picked]
        alert_s = entity_alerted[picked]
        etp = int((case_s & alert_s).sum())
        efp = int((~case_s & alert_s).sum())
        efn = int((case_s & ~alert_s).sum())
        samples["entity_precision"].append(etp / (etp + efp) if (etp + efp) else 0.0)
        samples["entity_recall"].append(etp / (etp + efn) if (etp + efn) else 0.0)

    # PR-AUC needs the full ranking rebuilt, so it gets fewer, cheaper draws.
    pr_iterations = min(iterations, 400)
    for _ in range(pr_iterations):
        picked = rng.integers(0, n_entities, size=n_entities)
        idx = np.concatenate([rows_by_entity[i] for i in picked])
        if y_bool[idx].sum() == 0:
            continue
        samples["pr_auc"].append(average_precision_score(y_bool[idx], risk_scores[idx]))

    def interval(values: list[float]) -> dict:
        array = np.asarray(values, dtype=float)
        return {
            "mean": float(array.mean()),
            "ci_low": float(np.percentile(array, 2.5)),
            "ci_high": float(np.percentile(array, 97.5)),
            "iterations": int(len(array)),
        }

    return {name: interval(values) for name, values in samples.items() if len(values)}


def ranking_metrics(risk_scores: np.ndarray, y_true: np.ndarray) -> dict:
    return {
        "pr_auc": float(average_precision_score(y_true, risk_scores)),
        "roc_auc": float(roc_auc_score(y_true, risk_scores)),
        "prevalence": float(np.mean(y_true)),
        "pr_auc_lift_over_random": float(
            average_precision_score(y_true, risk_scores) / np.mean(y_true)
        ),
    }


def cost_sensitivity(risk_scores: np.ndarray, y_true: np.ndarray, entity_ids: np.ndarray,
                     miss_values: list[float] | None = None,
                     level: str = "entity") -> list[dict]:
    """
    How far does the recommended threshold move when the invented cost of a
    missed case moves? If the optimum is stable across a 20x range, the
    recommendation survives the assumption being wrong.
    """
    miss_values = miss_values or cfg.COST_SENSITIVITY_MISS_VALUES
    results = []
    for miss_cost in miss_values:
        frame = sweep(risk_scores, y_true, entity_ids, cost_per_miss=miss_cost)
        threshold = cost_minimising_threshold(frame, level=level)
        row = frame.loc[frame["threshold"] == threshold].iloc[0]
        results.append(
            {
                "cost_per_missed_case_cad": miss_cost,
                "optimal_threshold": threshold,
                "precision": float(row[f"{level}_precision"]),
                "recall": float(row[f"{level}_recall"]),
                "expected_cost_cad": float(row[f"{level}_expected_cost_cad"]),
                "alerts": int(row[f"{level}_alerts"]),
            }
        )
    return results
