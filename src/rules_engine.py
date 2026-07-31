"""
Deterministic rules R1-R5, one per planted typology.

Every threshold here is LOOSER than the generator's. That is deliberate and it
is the single most important design decision in this file. A rule tuned to the
generator's exact parameters is a fingerprint of the generator: it would score
near-perfect precision and recall, and every number downstream would be
measuring the data-generating process rather than a detector. Loosening the
bands means the rules also fire on legitimate behaviour -- cash-intensive
retailers depositing in the same range, invoices that settle at round
thousands -- which is where the false positives, and therefore the honest
precision numbers, come from.

Each rule returns a boolean per transaction plus a human-readable reason code
an analyst could act on, e.g.

    R1_STRUCTURING: 4 cash deposits totalling $34,214.87 in 6 days,
    each between $7,500 and $10,000

Rules fire on every transaction in the pattern they detect, not just the one
that tipped the threshold, because an analyst opening the case needs the whole
cluster rather than its last leg.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)

import config as cfg  # noqa: E402

SORT_KEYS = ["entity_id", "txn_epoch", "transaction_id"]

REQUIRED_TRANSACTION_COLUMNS = [
    "transaction_id", "entity_id", "counterparty_id", "txn_epoch",
    "amount_cents", "direction", "is_cash",
]
REQUIRED_FEATURE_COLUMNS = [
    "transaction_id", "amount_zscore_entity", "is_round_amount",
    "days_since_prev_txn", "prior_txn_count", "prior_mean_cents",
]


def _money(cents: float) -> str:
    return f"${cents / 100:,.2f}"


def _entity_slices(entity_ids: np.ndarray) -> list[tuple[int, int]]:
    if len(entity_ids) == 0:
        return []
    codes = pd.factorize(entity_ids, sort=False)[0]
    cuts = np.flatnonzero(np.diff(codes)) + 1
    return list(
        zip(np.concatenate([[0], cuts]).tolist(), np.concatenate([cuts, [len(codes)]]).tolist())
    )


# ---------------------------------------------------------------------------
# R1 -- structuring
# ---------------------------------------------------------------------------
def rule_structuring(frame: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """
    Three or more cash deposits inside the reporting band within seven days.

    A deposit fires if it belongs to ANY qualifying seven-day window, not only
    one that ends on it. Anchoring the window on each deposit and looking
    forward means the first deposits of a cluster are flagged too -- a trailing
    window would structurally miss the opening n-1 deposits of every case and
    quietly cap recall.
    """
    n = len(frame)
    fired = np.zeros(n, bool)
    reasons = [""] * n

    floor_cents = int(round(cfg.R1_AMOUNT_FLOOR * 100))
    ceiling_cents = int(round(cfg.R1_AMOUNT_CEILING * 100))
    window = cfg.R1_WINDOW_DAYS * 86_400

    amounts = frame["amount_cents"].to_numpy(np.int64)
    epochs = frame["txn_epoch"].to_numpy(np.int64)
    candidate = (
        (frame["is_cash"].to_numpy() == 1)
        & (frame["direction"].to_numpy() == "inbound")
        & (amounts >= floor_cents)
        & (amounts <= ceiling_cents)
    )

    for start, stop in _entity_slices(frame["entity_id"].to_numpy()):
        local = np.flatnonzero(candidate[start:stop])
        if len(local) < cfg.R1_MIN_DEPOSITS:
            continue
        positions = local + start
        deposit_epochs = epochs[positions]

        best_count = np.zeros(len(positions), np.int64)
        best_span = np.zeros(len(positions), np.float64)
        best_total = np.zeros(len(positions), np.int64)

        for i in range(len(positions)):
            end = int(np.searchsorted(deposit_epochs, deposit_epochs[i] + window, "right"))
            count = end - i
            if count < cfg.R1_MIN_DEPOSITS:
                continue
            total = int(amounts[positions[i:end]].sum())
            span = (deposit_epochs[end - 1] - deposit_epochs[i]) / 86_400.0
            # Attribute each deposit to the largest window it belongs to, so
            # the reason code describes the full cluster.
            improved = count > best_count[i:end]
            idx = np.flatnonzero(improved) + i
            best_count[idx] = count
            best_span[idx] = span
            best_total[idx] = total

        hits = np.flatnonzero(best_count >= cfg.R1_MIN_DEPOSITS)
        for i in hits:
            row = positions[i]
            fired[row] = True
            reasons[row] = (
                f"R1_STRUCTURING: {best_count[i]} cash deposits totalling "
                f"{_money(best_total[i])} in {best_span[i]:.1f} days, each between "
                f"${cfg.R1_AMOUNT_FLOOR:,.0f} and $10,000"
            )
    return fired, reasons


# ---------------------------------------------------------------------------
# R2 -- rapid layering
# ---------------------------------------------------------------------------
def rule_layering(frame: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """
    A large credit dispersed to several different counterparties within 48
    hours, most of it leaving. Fires on the inbound leg and every outbound leg
    that carried the money out.
    """
    n = len(frame)
    fired = np.zeros(n, bool)
    reasons = [""] * n

    min_inbound = int(round(cfg.R2_MIN_INBOUND * 100))
    window = cfg.R2_WINDOW_HOURS * 3_600

    amounts = frame["amount_cents"].to_numpy(np.int64)
    epochs = frame["txn_epoch"].to_numpy(np.int64)
    is_outbound = frame["direction"].to_numpy() == "outbound"
    counterparties = frame["counterparty_id"].to_numpy()

    for start, stop in _entity_slices(frame["entity_id"].to_numpy()):
        local_epochs = epochs[start:stop]
        inbound_positions = np.flatnonzero(
            (~is_outbound[start:stop]) & (amounts[start:stop] >= min_inbound)
        )
        if not len(inbound_positions):
            continue

        for i in inbound_positions:
            end = int(np.searchsorted(local_epochs, local_epochs[i] + window, "right"))
            candidates = np.arange(i + 1, end)
            legs = candidates[is_outbound[start:stop][candidates]]
            if len(legs) < cfg.R2_MIN_LEGS:
                continue

            leg_counterparties = counterparties[start:stop][legs]
            distinct = len(set(leg_counterparties[leg_counterparties != ""]))
            if distinct < cfg.R2_MIN_DISTINCT_COUNTERPARTIES:
                continue

            inbound_amount = int(amounts[start:stop][i])
            outbound_total = int(amounts[start:stop][legs].sum())
            passthrough = outbound_total / inbound_amount
            if passthrough < cfg.R2_MIN_PASSTHROUGH:
                continue

            hours = (local_epochs[legs[-1]] - local_epochs[i]) / 3_600.0
            reason = (
                f"R2_LAYERING: {_money(inbound_amount)} inbound dispersed to "
                f"{distinct} counterparties in {len(legs)} transfers within "
                f"{hours:.0f}h, {passthrough:.0%} passed through"
            )
            for offset in np.concatenate([[i], legs]):
                fired[start + offset] = True
                reasons[start + offset] = reason
    return fired, reasons


# ---------------------------------------------------------------------------
# R3 -- smurfing
# ---------------------------------------------------------------------------
def rule_smurfing(frame: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """
    Many different senders paying similar modest amounts into one account
    inside 72 hours. "Similar" is a coefficient of variation test, which is
    scale-free -- an absolute spread would behave differently at $400 and at
    $4,000.
    """
    n = len(frame)
    fired = np.zeros(n, bool)
    reasons = [""] * n

    window = cfg.R3_WINDOW_HOURS * 3_600
    max_mean = cfg.R3_MAX_MEAN_AMOUNT * 100

    amounts = frame["amount_cents"].to_numpy(np.int64)
    epochs = frame["txn_epoch"].to_numpy(np.int64)
    counterparties = frame["counterparty_id"].to_numpy()
    inbound = (frame["direction"].to_numpy() == "inbound") & (counterparties != "")

    for start, stop in _entity_slices(frame["entity_id"].to_numpy()):
        local = np.flatnonzero(inbound[start:stop])
        if len(local) < cfg.R3_MIN_DISTINCT_SENDERS:
            continue
        positions = local + start
        credit_epochs = epochs[positions]
        credit_amounts = amounts[positions].astype(np.float64)
        credit_cps = counterparties[positions]

        for i in range(len(positions)):
            end = int(np.searchsorted(credit_epochs, credit_epochs[i] + window, "right"))
            if end - i < cfg.R3_MIN_DISTINCT_SENDERS:
                continue
            senders = set(credit_cps[i:end])
            if len(senders) < cfg.R3_MIN_DISTINCT_SENDERS:
                continue

            block = credit_amounts[i:end]
            mean = float(block.mean())
            if mean > max_mean:
                continue
            cv = float(block.std(ddof=0)) / mean if mean else np.inf
            if cv >= cfg.R3_MAX_AMOUNT_CV:
                continue

            hours = (credit_epochs[end - 1] - credit_epochs[i]) / 3_600.0
            reason = (
                f"R3_SMURFING: {len(senders)} distinct senders paid in "
                f"{_money(block.sum())} over {hours:.0f}h, amounts within "
                f"{cv:.0%} of each other (avg {_money(mean)})"
            )
            for row in positions[i:end]:
                fired[row] = True
                reasons[row] = reason
    return fired, reasons


# ---------------------------------------------------------------------------
# R4 -- round-dollar anomaly
# ---------------------------------------------------------------------------
def rule_round_dollar(frame: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """
    An exact $1,000 multiple that is also a genuine outlier against this
    entity's own history. The z-score condition is what makes it entity-
    relative: a $5,000 round payment is unremarkable for a wholesaler and
    striking for a gig worker.
    """
    zscore = frame["amount_zscore_entity"].to_numpy(np.float64)
    fired = (
        (frame["is_round_amount"].to_numpy() == 1)
        & (zscore >= cfg.R4_MIN_ZSCORE)
        & (frame["prior_txn_count"].to_numpy() >= cfg.R4_MIN_PRIOR_TXNS)
    )
    amounts = frame["amount_cents"].to_numpy(np.int64)
    reasons = [""] * len(frame)
    for row in np.flatnonzero(fired):
        reasons[row] = (
            f"R4_ROUND_DOLLAR: {_money(amounts[row])} is an exact $1,000 multiple "
            f"and {zscore[row]:.1f} standard deviations above this entity's own "
            "historical average"
        )
    return fired, reasons


# ---------------------------------------------------------------------------
# R5 -- dormant reactivation
# ---------------------------------------------------------------------------
def rule_dormant_reactivation(frame: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """A long silence broken by a transaction far larger than the norm."""
    gap_days = frame["days_since_prev_txn"].to_numpy(np.float64)
    prior_mean = frame["prior_mean_cents"].to_numpy(np.float64)
    amounts = frame["amount_cents"].to_numpy(np.int64)

    multiple = np.divide(
        amounts, prior_mean, out=np.zeros(len(frame)), where=prior_mean > 0
    )
    fired = (
        (gap_days >= cfg.R5_MIN_DORMANT_DAYS)
        & (prior_mean > 0)
        & (multiple >= cfg.R5_MIN_MULTIPLIER)
    )
    reasons = [""] * len(frame)
    for row in np.flatnonzero(fired):
        reasons[row] = (
            f"R5_DORMANT: {_money(amounts[row])} after {gap_days[row]:.0f} days of "
            f"no activity, {multiple[row]:.1f}x the entity's historical average of "
            f"{_money(prior_mean[row])}"
        )
    return fired, reasons


RULES = {
    "R1_STRUCTURING": rule_structuring,
    "R2_LAYERING": rule_layering,
    "R3_SMURFING": rule_smurfing,
    "R4_ROUND_DOLLAR": rule_round_dollar,
    "R5_DORMANT": rule_dormant_reactivation,
}


def apply_rules(transactions: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """
    Run every rule and return one row per transaction with the boolean hits,
    the number of rules fired, and the concatenated reason codes.
    """
    missing = set(REQUIRED_TRANSACTION_COLUMNS) - set(transactions.columns)
    assert not missing, f"transactions is missing {sorted(missing)}"
    missing = set(REQUIRED_FEATURE_COLUMNS) - set(features.columns)
    assert not missing, f"features is missing {sorted(missing)}"

    frame = (
        transactions[REQUIRED_TRANSACTION_COLUMNS]
        .merge(features[REQUIRED_FEATURE_COLUMNS], on="transaction_id", how="inner", validate="1:1")
        .sort_values(SORT_KEYS, kind="stable")
        .reset_index(drop=True)
    )
    assert len(frame) == len(transactions), "feature join dropped or duplicated rows"

    result = pd.DataFrame({"transaction_id": frame["transaction_id"]})
    all_reasons = [[] for _ in range(len(frame))]

    for code, rule in RULES.items():
        fired, reasons = rule(frame)
        result[code] = fired
        for row in np.flatnonzero(fired):
            all_reasons[row].append(reasons[row])

    result["n_rules_fired"] = result[list(RULES)].sum(axis=1).astype(int)
    result["reason_codes"] = [" | ".join(r) for r in all_reasons]
    return result


def summarise(rule_hits: pd.DataFrame, transactions: pd.DataFrame) -> pd.DataFrame:
    """Per-rule fire counts and precision against ground truth, for reporting."""
    labels = (
        transactions.set_index("transaction_id")["is_suspicious"]
        .reindex(rule_hits["transaction_id"])
        .to_numpy()
        .astype(bool)
    )
    rows = []
    for code in RULES:
        fired = rule_hits[code].to_numpy()
        hits = int((fired & labels).sum())
        rows.append(
            {
                "rule": code,
                "fired": int(fired.sum()),
                "true_positives": hits,
                "false_positives": int(fired.sum()) - hits,
                "precision": hits / fired.sum() if fired.sum() else 0.0,
                "recall_of_all_suspicious": hits / labels.sum() if labels.sum() else 0.0,
            }
        )
    return pd.DataFrame(rows)
