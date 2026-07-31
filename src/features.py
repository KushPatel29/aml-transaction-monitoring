"""
Python mirror of sql/02_features.sql (C2).

Every column here reimplements a definition from the SQL file, and
tests/test_sql_python_feature_parity.py asserts the two agree on all 100,299
rows. The point is not that pandas is faster -- it is that the SQL a reviewer
reads and the Python a reviewer reads compute provably the same thing, so
neither can drift without the build going red.

Two places demand care, and both are commented at the point of implementation:

  * RANGE window frames include *peers*. Two transactions sharing a timestamp
    are both inside each other's frame regardless of row order, which a naive
    `.rolling()` on row counts gets wrong. The trick is a right-inclusive
    searchsorted rather than a row offset.

  * The entity z-score uses the one-pass E[x^2] - E[x]^2 identity because that
    is what a single SQL window pass can express. It is the numerically weaker
    formula, so the parity test also checks it against a two-pass numpy
    computation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as cfg  # noqa: E402

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)

SORT_KEYS = ["entity_id", "txn_epoch", "transaction_id"]


def _entity_slices(entity_ids: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous [start, stop) ranges per entity. Input must be sorted."""
    if len(entity_ids) == 0:
        return []
    codes = pd.factorize(entity_ids, sort=False)[0]
    cuts = np.flatnonzero(np.diff(codes)) + 1
    starts = np.concatenate([[0], cuts])
    stops = np.concatenate([cuts, [len(codes)]])
    return list(zip(starts.tolist(), stops.tolist(), strict=True))


def _window_bounds(epochs: np.ndarray, seconds: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Frame bounds equivalent to
        RANGE BETWEEN <seconds> PRECEDING AND CURRENT ROW.

    `side="right"` on the upper bound is what makes ties peers: every row
    sharing this row's epoch is included, matching SQL's RANGE semantics
    rather than a positional ROWS frame.
    """
    lo = np.searchsorted(epochs, epochs - seconds, side="left")
    hi = np.searchsorted(epochs, epochs, side="right")
    return lo, hi


def _prefix(values: np.ndarray) -> np.ndarray:
    return np.concatenate([[values.dtype.type(0)], np.cumsum(values)])


def compute_features(transactions: pd.DataFrame) -> pd.DataFrame:
    df = transactions.sort_values(SORT_KEYS, kind="stable").reset_index(drop=True)
    n = len(df)

    epochs_all = df["txn_epoch"].to_numpy(np.int64)
    amounts_all = df["amount_cents"].to_numpy(np.int64)
    is_cash_all = df["is_cash"].to_numpy(np.int64)
    is_outbound_all = (df["direction"].to_numpy() == "outbound").astype(np.int64)
    counterparty_all = df["counterparty_id"].to_numpy()

    has_cp = counterparty_all != ""

    # ---- global counterparty statistics -----------------------------------
    # cp_first_seen: earliest appearance anywhere. Always <= the epoch of any
    # row referencing it, so this cannot leak the future.
    cp_first_seen = (
        df.loc[has_cp].groupby("counterparty_id")["txn_epoch"].min().to_dict()
    )

    # cp_entity_pairs: earliest epoch each (counterparty, entity) pair traded.
    pairs = (
        df.loc[has_cp]
        .groupby(["counterparty_id", "entity_id"])["txn_epoch"]
        .min()
        .reset_index()
    )
    pair_epochs_by_cp: dict[str, np.ndarray] = {
        cp_id: np.sort(group["txn_epoch"].to_numpy(np.int64))
        for cp_id, group in pairs.groupby("counterparty_id", sort=False)
    }

    # Per-row: when its counterparty was first seen. -inf for cash deposits so
    # they can never satisfy the "new counterparty" test, matching the inner
    # join in the SQL that drops them from that numerator.
    cp_first_of_row = np.full(n, -np.inf)
    cp_first_of_row[has_cp] = [cp_first_seen[c] for c in counterparty_all[has_cp]]

    # Integer codes make the per-window distinct count cheap; -1 means "no
    # counterparty", which the SQL excludes with `counterparty_id <> ''`.
    cp_codes = np.full(n, -1, dtype=np.int64)
    cp_codes[has_cp] = pd.factorize(counterparty_all[has_cp])[0]

    # ---- output buffers ----------------------------------------------------
    txn_count_7d = np.zeros(n, np.int64)
    txn_sum_7d = np.zeros(n, np.int64)
    txn_count_30d = np.zeros(n, np.int64)
    txn_sum_30d = np.zeros(n, np.int64)
    cash_sum_7d = np.zeros(n, np.int64)
    out_48h = np.zeros(n, np.int64)
    in_48h = np.zeros(n, np.int64)
    zscore = np.zeros(n, np.float64)
    prior_count = np.zeros(n, np.int64)
    prior_mean_cents = np.zeros(n, np.float64)
    days_since_prev = np.zeros(n, np.float64)
    distinct_cp_7d = np.zeros(n, np.int64)
    new_cp_count_7d = np.zeros(n, np.int64)

    for start, stop in _entity_slices(df["entity_id"].to_numpy()):
        epochs = epochs_all[start:stop]
        amounts = amounts_all[start:stop]
        size = stop - start

        lo7, hi = _window_bounds(epochs, cfg.WINDOW_7D_SECONDS)
        lo30, _ = _window_bounds(epochs, cfg.WINDOW_30D_SECONDS)
        lo48, _ = _window_bounds(epochs, cfg.WINDOW_48H_SECONDS)

        prefix_amount = _prefix(amounts)
        prefix_cash = _prefix(amounts * is_cash_all[start:stop])
        prefix_out = _prefix(amounts * is_outbound_all[start:stop])

        txn_count_7d[start:stop] = hi - lo7
        txn_sum_7d[start:stop] = prefix_amount[hi] - prefix_amount[lo7]
        txn_count_30d[start:stop] = hi - lo30
        txn_sum_30d[start:stop] = prefix_amount[hi] - prefix_amount[lo30]
        cash_sum_7d[start:stop] = prefix_cash[hi] - prefix_cash[lo7]

        out_window = prefix_out[hi] - prefix_out[lo48]
        total_48h = prefix_amount[hi] - prefix_amount[lo48]
        out_48h[start:stop] = out_window
        in_48h[start:stop] = total_48h - out_window

        # ---- entity-relative z-score over strictly prior rows --------------
        # Ordering is (txn_epoch, transaction_id), matching the SQL's wprior,
        # so "prior rows" is simply the prefix before position i.
        amounts_f = amounts.astype(np.float64)
        prefix_f = np.concatenate([[0.0], np.cumsum(amounts_f)])
        prefix_sq = np.concatenate([[0.0], np.cumsum(amounts_f * amounts_f)])
        prior_n = np.arange(size, dtype=np.float64)
        prior_sum = prefix_f[:-1]
        prior_sumsq = prefix_sq[:-1]

        safe_n = np.where(prior_n > 0, prior_n, 1.0)
        prior_mean = prior_sum / safe_n
        prior_var = prior_sumsq / safe_n - prior_mean * prior_mean
        prior_sd = np.sqrt(np.maximum(prior_var, 0.0))

        usable = (
            (prior_n >= cfg.ZSCORE_MIN_HISTORY)
            & (prior_var > 0)
            & (prior_sd >= cfg.ZSCORE_MIN_STDDEV)
        )
        zscore[start:stop] = np.where(
            usable, (amounts_f - prior_mean) / np.where(prior_sd > 0, prior_sd, 1.0), 0.0
        )
        prior_count[start:stop] = prior_n.astype(np.int64)
        # Matches COALESCE(prior_mean, 0.0): an entity's first transaction has
        # no prior history, and prior_sum is 0.0 there, so this lands on 0.0.
        prior_mean_cents[start:stop] = prior_mean

        # ---- days since previous ------------------------------------------
        gaps = np.empty(size, np.float64)
        gaps[0] = -1.0  # first transaction for this entity, not "zero days"
        if size > 1:
            gaps[1:] = (epochs[1:] - epochs[:-1]) / 86_400.0
        days_since_prev[start:stop] = gaps

        # ---- per-row window scans -----------------------------------------
        # COUNT(DISTINCT ...) has no window-function form in SQLite or
        # Postgres, so the SQL uses a correlated subquery and this uses a
        # bounded scan. Windows hold a handful of rows, so a Python set beats
        # np.unique here.
        codes = cp_codes[start:stop]
        firsts = cp_first_of_row[start:stop]
        codes_list = codes.tolist()
        firsts_list = firsts.tolist()
        lo7_list = lo7.tolist()
        hi_list = hi.tolist()
        epoch_list = epochs.tolist()

        for i in range(size):
            a, b = lo7_list[i], hi_list[i]
            cutoff = epoch_list[i] - cfg.NEW_COUNTERPARTY_WINDOW_SECONDS
            seen = set()
            fresh = 0
            for j in range(a, b):
                code = codes_list[j]
                if code >= 0:
                    seen.add(code)
                    if firsts_list[j] >= cutoff:
                        fresh += 1
            distinct_cp_7d[start + i] = len(seen)
            new_cp_count_7d[start + i] = fresh

    # ---- counterparty as-of degree ----------------------------------------
    degree_asof = np.zeros(n, np.int64)
    for cp_id, positions in df.groupby("counterparty_id", sort=False).indices.items():
        if cp_id == "":
            continue
        degree_asof[positions] = np.searchsorted(
            pair_epochs_by_cp[cp_id], epochs_all[positions], side="right"
        )

    counterparty_age = np.where(
        has_cp, (epochs_all - np.where(has_cp, cp_first_of_row, 0)) / 86_400.0, 0.0
    )

    # ---- assemble ----------------------------------------------------------
    timestamps = pd.to_datetime(df["txn_datetime"])

    out = pd.DataFrame(
        {
            "transaction_id": df["transaction_id"],
            "entity_id": df["entity_id"],
            "counterparty_id": df["counterparty_id"],
            "txn_epoch": df["txn_epoch"],
            "amount_cents": df["amount_cents"],
            "direction": df["direction"],
            "is_cash": df["is_cash"],
            "txn_count_7d": txn_count_7d,
            "txn_sum_7d_cents": txn_sum_7d,
            "txn_count_30d": txn_count_30d,
            "txn_sum_30d_cents": txn_sum_30d,
            "amount_zscore_entity": zscore,
            "is_round_amount": (amounts_all % cfg.ROUND_AMOUNT_CENTS == 0).astype(np.int64),
            "days_since_prev_txn": days_since_prev,
            "distinct_counterparties_7d": distinct_cp_7d,
            "outbound_to_inbound_ratio_48h": np.where(
                in_48h == 0,
                0.0,
                np.minimum(
                    out_48h / np.where(in_48h == 0, 1, in_48h).astype(np.float64),
                    cfg.OUT_IN_RATIO_CAP,
                ),
            ),
            "cash_ratio_7d": cash_sum_7d / txn_sum_7d.astype(np.float64),
            "counterparty_entity_degree_asof": degree_asof,
            "counterparty_age_days": counterparty_age,
            "new_counterparty_ratio_7d": new_cp_count_7d / txn_count_7d.astype(np.float64),
            # SQLite's strftime('%w') codes Sunday as 0 and Saturday as 6;
            # pandas codes Monday as 0 and Sunday as 6. Different encodings,
            # same weekend.
            "hour_of_day": timestamps.dt.hour.astype(np.int64),
            "is_weekend": timestamps.dt.dayofweek.isin([5, 6]).astype(np.int64),
            "prior_txn_count": prior_count,
            "prior_mean_cents": prior_mean_cents,
        }
    )
    return out


def zscore_two_pass(transactions: pd.DataFrame) -> np.ndarray:
    """
    Independent two-pass implementation of the entity z-score, used only by the
    parity test as a numerical control on the one-pass identity that SQL and
    compute_features() both use.
    """
    df = transactions.sort_values(SORT_KEYS, kind="stable").reset_index(drop=True)
    result = np.zeros(len(df), np.float64)
    amounts_all = df["amount_cents"].to_numpy(np.float64)

    for start, stop in _entity_slices(df["entity_id"].to_numpy()):
        amounts = amounts_all[start:stop]
        for i in range(stop - start):
            if i < cfg.ZSCORE_MIN_HISTORY:
                continue
            prior = amounts[:i]
            sd = prior.std(ddof=0)  # two-pass: mean first, then deviations
            if sd < cfg.ZSCORE_MIN_STDDEV:
                continue
            result[start + i] = (amounts[i] - prior.mean()) / sd
    return result
