"""
Unsupervised anomaly scoring, plus a supervised ceiling for comparison.

Three design decisions carry this module.

**Entity-disjoint cross-validation, not a single split.** Sixty planted cases
is far too few for one 70/30 split to say anything stable. Five entity-disjoint
folds, each fitted on its training entities and scoring only held-out ones,
pool into a single out-of-fold prediction covering all 100,299 transactions and
all 60 cases -- every one held out exactly once. Splitting by transaction
instead of entity would leak: the features are entity-relative, so an entity's
own history would sit on both sides of the boundary.

**A bake-off, not a model.** IsolationForest is the obvious choice, which is
exactly why it needs challengers. Local Outlier Factor and a plain Mahalanobis
distance run under identical conditions. If the textbook answer loses to the
statistics-101 answer, that is the finding and it goes in the README.

**The supervised model is a ceiling, not a product.** It is trained on the
ground-truth labels and reported as "what you would gain with clean historical
labels", which teams working real transaction monitoring rarely have. Shipping
it would be pretending the labels exist.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)

import config as cfg  # noqa: E402

UNSUPERVISED_MODELS = ["isolation_forest", "local_outlier_factor", "mahalanobis"]
SUPERVISED_MODELS = ["supervised_logistic", "supervised_gbm"]
ALL_MODELS = UNSUPERVISED_MODELS + SUPERVISED_MODELS


@dataclass
class ModelRun:
    scores: pd.DataFrame
    fold_assignment: pd.DataFrame
    timings: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------
def assign_entity_folds(entity_ids: pd.Series, has_case: pd.Series,
                        n_folds: int = cfg.N_CV_FOLDS,
                        seed: int = cfg.CV_SEED) -> pd.DataFrame:
    """
    Deterministic entity-disjoint folds, balanced on whether the entity carries
    a planted case.

    Balancing uses the label only to decide *where the fold boundaries fall*,
    never as a model input -- the standard stratified-group split. With 60 cases
    across 5 folds, leaving it to chance would routinely hand one fold 6 cases
    and another 18, and the per-fold metrics would be noise.
    """
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame({"entity_id": entity_ids, "has_case": has_case}).drop_duplicates()
    frame = frame.sort_values("entity_id").reset_index(drop=True)

    folds = np.empty(len(frame), dtype=np.int64)
    for flag in (True, False):
        mask = frame["has_case"].to_numpy() == flag
        positions = np.flatnonzero(mask)
        shuffled = rng.permutation(positions)
        folds[shuffled] = np.arange(len(shuffled)) % n_folds
    frame["fold"] = folds
    return frame[["entity_id", "fold"]]


# ---------------------------------------------------------------------------
# Score normalisation
# ---------------------------------------------------------------------------
def _percentile_against(reference: np.ndarray, values: np.ndarray) -> np.ndarray:
    """
    Map raw scores onto [0, 1] by their rank within the TRAINING distribution.

    Normalising against the training fold rather than the test fold matters:
    percentile-ranking the test scores among themselves would force a fixed
    fraction of every batch to look anomalous, no matter how quiet the batch
    actually was.
    """
    ordered = np.sort(reference)
    return np.searchsorted(ordered, values, side="right") / max(len(ordered), 1)


# ---------------------------------------------------------------------------
# Individual scorers -- all return "higher means more anomalous"
# ---------------------------------------------------------------------------
def _isolation_forest(train: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    model = IsolationForest(**cfg.ISOLATION_FOREST_PARAMS).fit(train)
    return -model.score_samples(train), -model.score_samples(test)


def _local_outlier_factor(train: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    model = LocalOutlierFactor(**cfg.LOF_PARAMS).fit(train)
    # negative_outlier_factor_ is the training-set score; novelty mode forbids
    # calling score_samples on the training data itself.
    return -model.negative_outlier_factor_, -model.score_samples(test)


def _mahalanobis(train: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    The statistics-101 challenger: squared distance from the training centroid
    under the training covariance. No hyperparameters, no trees, no neighbours.
    """
    mean = train.mean(axis=0)
    covariance = np.cov(train, rowvar=False)
    # Ridge the diagonal -- several features are near-constant on some folds
    # (is_weekend on a business-heavy fold, for instance) and the raw
    # covariance is singular.
    covariance += np.eye(covariance.shape[0]) * 1e-6
    inverse = np.linalg.pinv(covariance)

    def distance(matrix: np.ndarray) -> np.ndarray:
        centred = matrix - mean
        return np.einsum("ij,jk,ik->i", centred, inverse, centred)

    return distance(train), distance(test)


def _supervised(train: np.ndarray, test: np.ndarray, y_train: np.ndarray,
                kind: str) -> tuple[np.ndarray, np.ndarray]:
    if kind == "supervised_logistic":
        model = LogisticRegression(**cfg.SUPERVISED_PARAMS)
    else:
        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.1, random_state=cfg.RANDOM_SEED
        )
    model.fit(train, y_train)
    return model.predict_proba(train)[:, 1], model.predict_proba(test)[:, 1]


# ---------------------------------------------------------------------------
# Cross-validated run
# ---------------------------------------------------------------------------
def run_models(features: pd.DataFrame, transactions: pd.DataFrame,
               verbose: bool = True) -> ModelRun:
    labels_by_txn = transactions.set_index("transaction_id")["is_suspicious"]
    frame = features.copy()
    frame["is_suspicious"] = labels_by_txn.reindex(frame["transaction_id"]).to_numpy()

    entity_has_case = frame.groupby("entity_id")["is_suspicious"].max().astype(bool)
    fold_map = assign_entity_folds(
        pd.Series(entity_has_case.index), pd.Series(entity_has_case.to_numpy())
    )
    frame = frame.merge(fold_map, on="entity_id", how="left", validate="m:1")
    assert frame["fold"].notna().all(), "an entity was left without a fold"

    matrix = frame[cfg.MODEL_FEATURE_COLUMNS].to_numpy(np.float64)
    assert np.isfinite(matrix).all(), "non-finite value reached the feature matrix"
    labels = frame["is_suspicious"].to_numpy().astype(int)
    folds = frame["fold"].to_numpy().astype(int)

    scores = {name: np.zeros(len(frame)) for name in ALL_MODELS}
    timings: dict[str, float] = {name: 0.0 for name in ALL_MODELS}

    for fold in range(cfg.N_CV_FOLDS):
        is_test = folds == fold
        is_train = ~is_test

        scaler = StandardScaler().fit(matrix[is_train])
        train = scaler.transform(matrix[is_train])
        test = scaler.transform(matrix[is_test])

        for name in ALL_MODELS:
            started = time.perf_counter()
            if name == "isolation_forest":
                train_scores, test_scores = _isolation_forest(train, test)
            elif name == "local_outlier_factor":
                train_scores, test_scores = _local_outlier_factor(train, test)
            elif name == "mahalanobis":
                train_scores, test_scores = _mahalanobis(train, test)
            else:
                train_scores, test_scores = _supervised(
                    train, test, labels[is_train], name
                )
            scores[name][is_test] = _percentile_against(train_scores, test_scores)
            timings[name] += time.perf_counter() - started

        if verbose:
            print(f"    fold {fold + 1}/{cfg.N_CV_FOLDS} scored "
                  f"({is_test.sum():,} held-out transactions)")

    result = pd.DataFrame({"transaction_id": frame["transaction_id"], "fold": folds})
    for name in ALL_MODELS:
        result[f"{name}_score"] = scores[name]

    return ModelRun(
        scores=result,
        fold_assignment=fold_map,
        timings=timings,
    )


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------
def top_feature_deviations(features: pd.DataFrame, transaction_id: str,
                           top_n: int = 4) -> pd.DataFrame:
    """
    Which features make this transaction unusual against the whole population.

    Deliberately not SHAP. At this scale a standardised deviation profile says
    the same thing an analyst needs -- "this row is 6.2 sigma high on
    distinct_counterparties_7d" -- without adding a heavy dependency to a
    Streamlit Community Cloud image. The README records that as a choice rather
    than an oversight.
    """
    matrix = features[cfg.MODEL_FEATURE_COLUMNS]
    means = matrix.mean()
    stds = matrix.std(ddof=0).replace(0, np.nan)

    row = features.loc[features["transaction_id"] == transaction_id, cfg.MODEL_FEATURE_COLUMNS]
    if row.empty:
        return pd.DataFrame(columns=["feature", "value", "population_mean", "deviation_sigma"])

    deviation = ((row.iloc[0] - means) / stds).fillna(0.0)
    ordered = deviation.abs().sort_values(ascending=False).head(top_n).index
    return pd.DataFrame(
        {
            "feature": ordered,
            "value": [row.iloc[0][f] for f in ordered],
            "population_mean": [means[f] for f in ordered],
            "deviation_sigma": [deviation[f] for f in ordered],
        }
    )
