"""
Does the generator actually plant signal?

Every typology below is asserted against the *definition* it claims to
implement, not against the constants that produced it -- so if someone widens
STRUCTURING_AMOUNT_MAX past the reporting threshold, or lets a round-dollar
case fall below the entity's own p90, these fail.

The last two tests point the other way. A generator that plants patterns a
single `amount > X` rule can recover has not built a detection problem, it has
built a lookup table, and every downstream metric would be meaningless.
"""

from __future__ import annotations

import numpy as np
import pytest

import config as cfg

SECONDS_PER_DAY = 86_400


def _case_groups(transactions, typology):
    rows = transactions[transactions["typology"] == typology]
    assert not rows.empty, f"no {typology} transactions were planted"
    return rows.groupby("case_id")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_transaction_volume_is_near_target(transactions):
    drift = abs(len(transactions) - cfg.TARGET_TRANSACTIONS) / cfg.TARGET_TRANSACTIONS
    assert drift <= cfg.TRANSACTION_COUNT_TOLERANCE, (
        f"{len(transactions):,} transactions is {drift:.1%} from the "
        f"{cfg.TARGET_TRANSACTIONS:,} target"
    )


def test_schema_is_complete_and_non_null(transactions):
    required = {
        "transaction_id", "entity_id", "counterparty_id", "txn_datetime",
        "txn_epoch", "amount_cents", "amount_cad", "direction", "channel",
        "is_cash", "is_suspicious", "typology", "typology_variant", "case_id",
    }
    assert required <= set(transactions.columns)
    assert transactions["transaction_id"].is_unique
    assert transactions[["entity_id", "txn_epoch", "amount_cents"]].notna().all().all()
    assert (transactions["amount_cents"] > 0).all()
    assert transactions["direction"].isin(["inbound", "outbound"]).all()
    assert transactions["channel"].isin(cfg.CHANNELS).all()
    # Cash deposits have no counterparty; everything else has one.
    assert (transactions.loc[transactions["channel"] == "cash_deposit",
                             "counterparty_id"] == "").all()
    assert (transactions.loc[transactions["channel"] != "cash_deposit",
                             "counterparty_id"] != "").all()


def test_planted_case_rate_matches_config(transactions, entities, cases):
    flagged_entities = cases["entity_id"].nunique()
    expected = round(len(entities) * cfg.TYPOLOGY_ENTITY_RATE)
    assert flagged_entities == expected
    # One typology per entity keeps case attribution unambiguous.
    assert cases["entity_id"].is_unique
    # Ground truth is internally consistent.
    labelled = transactions[transactions["is_suspicious"] == 1]
    assert (labelled["typology"] != "").all()
    assert (labelled["case_id"] != "").all()
    assert (transactions[transactions["is_suspicious"] == 0]["typology"] == "").all()


# ---------------------------------------------------------------------------
# T1 -- structuring
# ---------------------------------------------------------------------------
def test_structuring_sits_below_the_reporting_threshold(transactions):
    rows = transactions[transactions["typology"] == "structuring"]
    assert (rows["amount_cad"] >= cfg.STRUCTURING_AMOUNT_MIN).all()
    assert (rows["amount_cad"] < 10_000).all(), "a structured deposit reached $10,000"
    assert (rows["is_cash"] == 1).all()
    assert (rows["direction"] == "inbound").all()

    counts = rows.groupby("case_id").size()
    assert counts.between(cfg.STRUCTURING_MIN_DEPOSITS, cfg.STRUCTURING_MAX_DEPOSITS).all()


def test_structuring_variants_straddle_the_seven_day_window(transactions):
    """
    The fast variant must fit inside a 7-day window and the slow variant must
    not. The slow subset is the planted stress case for R1's fixed window --
    if it ever drifts back inside 7 days, the limitation reported in the README
    stops being real.
    """
    rows = transactions[transactions["typology"] == "structuring"]
    spans = (
        rows.groupby(["case_id", "typology_variant"])["txn_epoch"]
        .agg(lambda s: (s.max() - s.min()) / SECONDS_PER_DAY)
        .reset_index(name="span_days")
    )

    fast = spans[spans["typology_variant"] == "fast"]["span_days"]
    slow = spans[spans["typology_variant"] == "slow"]["span_days"]
    assert not fast.empty and not slow.empty

    assert (fast < cfg.STRUCTURING_FAST_WINDOW_DAYS).all(), (
        f"a 'fast' case spans {fast.max():.2f} days, outside the "
        f"{cfg.STRUCTURING_FAST_WINDOW_DAYS}-day window it is supposed to fit in"
    )
    assert (slow > cfg.STRUCTURING_FAST_WINDOW_DAYS).all(), (
        f"a 'slow' case spans only {slow.min():.2f} days -- it would be caught "
        "by the 7-day rule and is no longer a stress case"
    )


# ---------------------------------------------------------------------------
# T2 -- rapid layering
# ---------------------------------------------------------------------------
def test_layering_passes_through_over_ninety_percent(transactions):
    for case_id, group in _case_groups(transactions, "layering"):
        inbound = group.loc[group["direction"] == "inbound", "amount_cad"].sum()
        outbound = group.loc[group["direction"] == "outbound", "amount_cad"].sum()
        legs = group[group["direction"] == "outbound"]

        assert inbound >= cfg.LAYERING_LUMP_MIN, case_id
        assert len(legs) >= 3, f"{case_id} has only {len(legs)} outbound legs"
        assert legs["counterparty_id"].nunique() == len(legs), (
            f"{case_id} reused a counterparty; legs must be distinct"
        )
        retained = 1 - outbound / inbound
        assert retained < 0.10, f"{case_id} retained {retained:.1%}, should be <10%"

        span_hours = (group["txn_epoch"].max() - group["txn_epoch"].min()) / 3_600
        assert span_hours <= 48, f"{case_id} spans {span_hours:.0f}h, over the 48h claim"


# ---------------------------------------------------------------------------
# T3 -- smurfing
# ---------------------------------------------------------------------------
def test_smurfing_uses_many_distinct_senders_in_seventy_two_hours(transactions):
    for case_id, group in _case_groups(transactions, "smurfing"):
        assert group["counterparty_id"].nunique() >= 10, (
            f"{case_id} has {group['counterparty_id'].nunique()} distinct senders"
        )
        assert (group["direction"] == "inbound").all()
        span_hours = (group["txn_epoch"].max() - group["txn_epoch"].min()) / 3_600
        assert span_hours <= cfg.SMURFING_WINDOW_HOURS, case_id
        # "Similar small amounts" -- tight dispersion, modest size.
        cv = group["amount_cad"].std(ddof=0) / group["amount_cad"].mean()
        assert cv < 0.25, f"{case_id} amount CV {cv:.2f} is not 'similar amounts'"
        assert group["amount_cad"].mean() <= cfg.SMURFING_MEAN_MAX * 1.2


# ---------------------------------------------------------------------------
# T4 -- round-dollar anomaly
# ---------------------------------------------------------------------------
def test_round_dollar_amounts_exceed_the_entitys_own_distribution(transactions):
    """
    The typology is defined *relative to the entity*, so a flat threshold is
    not enough: each planted amount has to be round AND above that entity's own
    historical p90.
    """
    for case_id, group in _case_groups(transactions, "round_dollar"):
        entity_id = group["entity_id"].iloc[0]
        baseline = transactions[
            (transactions["entity_id"] == entity_id) & (transactions["typology"] == "")
        ]
        assert len(baseline) >= 10, f"{case_id} entity has too little history to be anomalous"

        assert (group["amount_cents"] % cfg.ROUND_AMOUNT_CENTS == 0).all(), (
            f"{case_id} contains a non-round amount"
        )
        p90 = baseline["amount_cad"].quantile(0.90)
        assert (group["amount_cad"] > p90).all(), (
            f"{case_id} has an amount at or below the entity's own p90 (${p90:,.0f})"
        )


# ---------------------------------------------------------------------------
# T5 -- dormant reactivation
# ---------------------------------------------------------------------------
def test_dormant_reactivation_follows_real_silence(transactions):
    for case_id, group in _case_groups(transactions, "dormant_reactivation"):
        entity_id = group["entity_id"].iloc[0]
        history = transactions[
            (transactions["entity_id"] == entity_id) & (transactions["typology"] == "")
        ]
        first = group["txn_epoch"].min()
        prior = history[history["txn_epoch"] < first]
        assert len(prior) >= 5, f"{case_id} has no history to be dormant against"

        gap_days = (first - prior["txn_epoch"].max()) / SECONDS_PER_DAY
        assert gap_days >= cfg.DORMANT_MIN_GAP_DAYS, (
            f"{case_id} reactivated after only {gap_days:.1f} days"
        )
        multiple = group["amount_cad"].min() / prior["amount_cad"].mean()
        assert multiple >= 5.0, f"{case_id} moved only {multiple:.1f}x its historical mean"


# ---------------------------------------------------------------------------
# The other direction: is this actually a hard problem?
# ---------------------------------------------------------------------------
def test_suspicious_activity_is_not_separable_by_amount_alone(transactions):
    """
    Sweep every possible `flag if amount > t` threshold and take the best F1
    any of them achieves. If a single amount cut could solve this, the whole
    layered system would be ceremony and every metric downstream would be
    measuring the generator instead of the detector.
    """
    amounts = transactions["amount_cad"].to_numpy()
    labels = transactions["is_suspicious"].to_numpy().astype(bool)
    total_positive = labels.sum()

    order = np.argsort(-amounts, kind="stable")
    hits = np.cumsum(labels[order])
    flagged = np.arange(1, len(order) + 1)
    precision = hits / flagged
    recall = hits / total_positive
    f1 = np.divide(2 * precision * recall, precision + recall,
                   out=np.zeros_like(precision), where=(precision + recall) > 0)

    best = float(f1.max())
    best_at = amounts[order][int(f1.argmax())]
    print(
        f"\n  best amount-only F1 = {best:.3f} at ${best_at:,.2f} "
        f"(precision {precision[int(f1.argmax())]:.3f}, "
        f"recall {recall[int(f1.argmax())]:.3f})"
    )
    assert best < 0.30, (
        f"a single amount threshold reaches F1={best:.3f}; the planted patterns "
        "are too easy for the rest of the pipeline to be meaningful"
    )


def test_legitimate_activity_generates_real_false_positive_pressure(transactions):
    """
    Honest precision numbers need honest noise. Cash-intensive businesses must
    deposit inside R1's band, and ordinary invoices must sometimes settle at
    round thousands -- otherwise R1 and R4 are free wins.
    """
    legit = transactions[transactions["typology"] == ""]

    in_band = legit[
        (legit["channel"] == "cash_deposit")
        & legit["amount_cad"].between(cfg.R1_AMOUNT_FLOOR, cfg.R1_AMOUNT_CEILING)
    ]
    assert in_band["entity_id"].nunique() >= 20, (
        f"only {in_band['entity_id'].nunique()} legitimate entities deposit inside "
        "R1's band -- structuring precision would be unrealistically flattering"
    )

    round_legit = (legit["amount_cents"] % cfg.ROUND_AMOUNT_CENTS == 0).sum()
    assert round_legit >= 100, (
        f"only {round_legit} legitimate round-thousand amounts -- R4 would be a free win"
    )

    over_threshold = (legit["amount_cad"] > cfg.NAIVE_BASELINE_THRESHOLD_CAD).sum()
    assert over_threshold >= 1_000, (
        "too few legitimate transactions above $10,000 for the naive baseline "
        "comparison to be a fair fight"
    )


@pytest.mark.parametrize("typology", sorted(cfg.TYPOLOGY_TOGGLES))
def test_every_enabled_typology_is_present(transactions, typology):
    if not cfg.TYPOLOGY_TOGGLES[typology]:
        pytest.skip(f"{typology} is toggled off")
    assert (transactions["typology"] == typology).any()
