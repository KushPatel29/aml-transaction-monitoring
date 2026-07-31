"""
C2: every feature computed in SQL has a Python equivalent that produces the
same value on the same input.

This runs on the full generated set -- all 100,299 rows, all 15 features --
rather than a sample. A parity contract checked on a sample is a parity
contract that fails in production on the rows the sample missed.

The z-score gets a third opinion. SQL and src/features.py both use the
one-pass E[x^2] - E[x]^2 identity, because that is what a single SQL window
pass can express, and it is the numerically weaker of the two formulas. So the
test also compares against a two-pass numpy computation. If catastrophic
cancellation ever bites at these magnitudes, agreement between the first two
would hide it and disagreement with the third would not.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import config as cfg
from src import db
from src import features as feature_module

ABS_TOLERANCE = 1e-6


@pytest.fixture(scope="module")
def parity_frames(transactions):
    """Build the SQL feature table and the pandas one from the same input."""
    conn, _ = db.build_warehouse()
    try:
        sql_features, _ = db.build_features(conn)
    finally:
        conn.close()

    python_features = feature_module.compute_features(transactions)

    sql_sorted = sql_features.sort_values("transaction_id").reset_index(drop=True)
    py_sorted = python_features.sort_values("transaction_id").reset_index(drop=True)
    return sql_sorted, py_sorted


def test_both_engines_produce_the_same_rows(parity_frames, transactions):
    sql_features, python_features = parity_frames
    assert len(sql_features) == len(transactions)
    assert len(python_features) == len(transactions)
    assert sql_features["transaction_id"].equals(python_features["transaction_id"])


@pytest.mark.parametrize("feature", cfg.PARITY_COLUMNS)
def test_feature_matches_between_sql_and_python(parity_frames, feature):
    sql_features, python_features = parity_frames
    assert feature in sql_features.columns, f"{feature} missing from sql/02_features.sql"
    assert feature in python_features.columns, f"{feature} missing from src/features.py"

    left = sql_features[feature].to_numpy(dtype=np.float64)
    right = python_features[feature].to_numpy(dtype=np.float64)

    mismatched = ~np.isclose(left, right, atol=ABS_TOLERANCE, rtol=0)
    if mismatched.any():
        idx = np.flatnonzero(mismatched)[:5]
        detail = "\n".join(
            f"    {sql_features['transaction_id'].iloc[i]}: "
            f"sql={left[i]!r}  python={right[i]!r}  diff={left[i] - right[i]!r}"
            for i in idx
        )
        pytest.fail(
            f"{feature}: {mismatched.sum()} of {len(left)} rows disagree\n{detail}"
        )


def test_integer_features_are_exactly_equal(parity_frames):
    """
    Counts and flags are integers on both sides, so "close enough" is not the
    standard -- they must be identical.
    """
    sql_features, python_features = parity_frames
    exact = [
        "txn_count_7d", "txn_sum_7d_cents", "txn_count_30d", "txn_sum_30d_cents",
        "is_round_amount", "distinct_counterparties_7d",
        "counterparty_entity_degree_asof", "hour_of_day", "is_weekend",
    ]
    for feature in exact:
        assert (
            sql_features[feature].to_numpy() == python_features[feature].to_numpy()
        ).all(), f"{feature} is not exactly equal between engines"


def test_zscore_one_pass_agrees_with_two_pass(parity_frames, transactions):
    """
    Numerical control on the one-pass variance identity. Amounts are in cents,
    so squares reach ~1e12 and the subtraction in E[x^2] - E[x]^2 loses
    precision in principle. This measures how much.
    """
    sql_features, _ = parity_frames

    two_pass = feature_module.zscore_two_pass(transactions)
    ordering = transactions.sort_values(
        feature_module.SORT_KEYS, kind="stable"
    )["transaction_id"].reset_index(drop=True)
    two_pass_series = (
        pd.Series(two_pass, index=ordering).sort_index().to_numpy()
    )
    one_pass = (
        sql_features.set_index("transaction_id")["amount_zscore_entity"]
        .sort_index()
        .to_numpy()
    )

    worst = float(np.max(np.abs(one_pass - two_pass_series)))
    print(f"\n  worst one-pass vs two-pass z-score deviation: {worst:.3e}")
    assert worst < 1e-6, (
        f"one-pass variance drifts from two-pass by {worst:.3e} -- cancellation "
        "is biting and sql/02_features.sql needs a numerically stabler form"
    )


def test_feature_definitions_hold_on_their_own_terms(parity_frames):
    """
    Agreement between two implementations of the same mistake is still a
    mistake, so check the invariants each feature claims independently.
    """
    sql_features, _ = parity_frames
    f = sql_features

    assert (f["txn_count_7d"] >= 1).all(), "a row is missing from its own 7-day frame"
    assert (f["txn_count_30d"] >= f["txn_count_7d"]).all()
    assert (f["txn_sum_30d_cents"] >= f["txn_sum_7d_cents"]).all()
    assert (f["amount_cents"] <= f["txn_sum_7d_cents"]).all()

    assert f["cash_ratio_7d"].between(0.0, 1.0).all()
    assert f["new_counterparty_ratio_7d"].between(0.0, 1.0).all()
    assert f["outbound_to_inbound_ratio_48h"].between(0.0, cfg.OUT_IN_RATIO_CAP).all()
    assert f["hour_of_day"].between(0, 23).all()
    assert f["is_weekend"].isin([0, 1]).all()
    assert f["is_round_amount"].isin([0, 1]).all()

    # -1.0 is the sentinel for "first transaction"; nothing else may be negative.
    gaps = f["days_since_prev_txn"]
    assert ((gaps >= 0) | (gaps == -1.0)).all()
    assert (gaps == -1.0).sum() == f["entity_id"].nunique()

    assert (f["counterparty_age_days"] >= 0).all()
    assert (f["distinct_counterparties_7d"] <= f["txn_count_7d"]).all()

    # Cash deposits have no counterparty, so their counterparty-derived
    # features must be the documented zero, not an accidental value.
    cash = f[f["counterparty_id"] == ""]
    assert (cash["counterparty_entity_degree_asof"] == 0).all()
    assert (cash["counterparty_age_days"] == 0.0).all()


def test_counterparty_degree_is_as_of_and_never_looks_ahead(parity_frames):
    """
    The single most dangerous feature in the set. A full-period counterparty
    degree would tell an early transaction how popular its counterparty
    eventually became -- future information, and a silent inflation of every
    downstream metric. As-of degree must be monotonically non-decreasing in
    time for a given counterparty, and must never exceed the final degree.
    """
    sql_features, _ = parity_frames
    with_cp = sql_features[sql_features["counterparty_id"] != ""]

    final_degree = (
        with_cp.groupby("counterparty_id")["entity_id"].nunique().rename("final_degree")
    )
    joined = with_cp.join(final_degree, on="counterparty_id")
    assert (joined["counterparty_entity_degree_asof"] <= joined["final_degree"]).all(), (
        "as-of degree exceeds the counterparty's final degree -- it is reading ahead"
    )

    ordered = with_cp.sort_values(["counterparty_id", "txn_epoch"])
    diffs = ordered.groupby("counterparty_id")["counterparty_entity_degree_asof"].diff()
    assert (diffs.dropna() >= 0).all(), "as-of degree decreases over time"

    # The last transaction of each counterparty must have caught up to the
    # final degree, otherwise the feature is systematically undercounting.
    last = ordered.groupby("counterparty_id").tail(1).join(final_degree, on="counterparty_id")
    assert (last["counterparty_entity_degree_asof"] == last["final_degree"]).all()
