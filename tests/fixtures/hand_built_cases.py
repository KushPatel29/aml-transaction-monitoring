"""
Hand-built transaction sets for testing R1-R5 in isolation.

Each rule gets at least one case it must fire on and several near-misses it
must stay silent on. The near-misses are the interesting half: they sit just
the wrong side of one condition each, so a rule that quietly drops a clause
still passes its positive case and fails here.

Nothing in this module touches the generated dataset. These are a few dozen
rows built by hand, so a failure points at the rule rather than at the data.
"""

from __future__ import annotations

import pandas as pd

BASE_EPOCH = int(pd.Timestamp("2026-01-05 00:00:00").value // 10**9)  # a Monday

COLUMNS = [
    "transaction_id", "entity_id", "counterparty_id", "txn_datetime", "txn_epoch",
    "amount_cents", "amount_cad", "direction", "channel", "is_cash",
    "is_suspicious", "typology", "typology_variant", "case_id",
]


def txn(entity_id, day, hour, amount_cad, direction="inbound", channel="eft",
        counterparty="CPT-00001"):
    is_cash = 1 if channel == "cash_deposit" else 0
    return {
        "entity_id": entity_id,
        "counterparty_id": "" if is_cash else counterparty,
        "txn_epoch": BASE_EPOCH + day * 86_400 + hour * 3_600,
        "amount_cents": int(round(amount_cad * 100)),
        "direction": direction,
        "channel": channel,
        "is_cash": is_cash,
    }


def build(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows).sort_values(["entity_id", "txn_epoch"], kind="stable")
    frame = frame.reset_index(drop=True)
    frame["transaction_id"] = [f"FIX-{i + 1:05d}" for i in range(len(frame))]
    frame["txn_datetime"] = pd.to_datetime(frame["txn_epoch"], unit="s").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    frame["amount_cad"] = (frame["amount_cents"] / 100.0).round(2)
    for column in ("is_suspicious",):
        frame[column] = 0
    for column in ("typology", "typology_variant", "case_id"):
        frame[column] = ""
    return frame[COLUMNS]


def _history(entity_id, n=15, amount=200.0, step=1, start_day=0, jitter=True):
    """Ordinary prior activity, so entity-relative rules have a baseline."""
    return [
        txn(entity_id, start_day + i * step, 10,
            amount + (i % 5) * 8.37 if jitter else amount,
            direction="outbound", counterparty=f"CPT-{100 + i:05d}")
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# R1 -- structuring
# ---------------------------------------------------------------------------
def structuring_fires():
    """Four cash deposits inside the band, inside seven days."""
    return build([
        txn("E-R1", 0, 10, 8_200.00, channel="cash_deposit"),
        txn("E-R1", 2, 11, 9_100.00, channel="cash_deposit"),
        txn("E-R1", 4, 14, 8_800.00, channel="cash_deposit"),
        txn("E-R1", 6, 15, 9_500.00, channel="cash_deposit"),
    ])


def structuring_too_few():
    """Same band, same window, but only two deposits."""
    return build([
        txn("E-R1b", 0, 10, 8_200.00, channel="cash_deposit"),
        txn("E-R1b", 3, 11, 9_100.00, channel="cash_deposit"),
    ])


def structuring_too_slow():
    """Four deposits in the band, but spread over twenty-four days."""
    return build([
        txn("E-R1c", 0, 10, 8_200.00, channel="cash_deposit"),
        txn("E-R1c", 8, 11, 9_100.00, channel="cash_deposit"),
        txn("E-R1c", 16, 14, 8_800.00, channel="cash_deposit"),
        txn("E-R1c", 24, 15, 9_500.00, channel="cash_deposit"),
    ])


def structuring_not_cash():
    """Four inbound transfers in the band -- but electronic, not cash."""
    return build([
        txn("E-R1d", 0, 10, 8_200.00, channel="eft"),
        txn("E-R1d", 2, 11, 9_100.00, channel="eft"),
        txn("E-R1d", 4, 14, 8_800.00, channel="eft"),
        txn("E-R1d", 6, 15, 9_500.00, channel="eft"),
    ])


def structuring_above_band():
    """Four cash deposits, all comfortably over the reporting threshold."""
    return build([
        txn("E-R1e", 0, 10, 14_200.00, channel="cash_deposit"),
        txn("E-R1e", 2, 11, 19_100.00, channel="cash_deposit"),
        txn("E-R1e", 4, 14, 12_800.00, channel="cash_deposit"),
        txn("E-R1e", 6, 15, 21_500.00, channel="cash_deposit"),
    ])


# ---------------------------------------------------------------------------
# R2 -- layering
# ---------------------------------------------------------------------------
def _layering(entity_id, inbound, legs, hours_apart=6, counterparties=None):
    counterparties = counterparties or [f"CPT-{200 + i:05d}" for i in range(len(legs))]
    rows = [txn(entity_id, 0, 9, inbound, direction="inbound", channel="wire",
                counterparty="CPT-00999")]
    for i, amount in enumerate(legs):
        rows.append(
            txn(entity_id, 0, 9 + (i + 1) * hours_apart, amount,
                direction="outbound", channel="wire", counterparty=counterparties[i])
        )
    return build(rows)


def layering_fires():
    """$80,000 in, 95% straight back out to four different counterparties."""
    return _layering("E-R2", 80_000.00, [22_000.0, 19_000.0, 18_000.0, 17_000.0])


def layering_retains_too_much():
    """Same shape, but only 30% leaves -- ordinary treasury movement."""
    return _layering("E-R2b", 80_000.00, [8_000.0, 8_000.0, 8_000.0])


def layering_same_counterparty():
    """95% leaves, but all of it to one counterparty. Not dispersion."""
    return _layering(
        "E-R2c", 80_000.00, [26_000.0, 25_000.0, 25_000.0],
        counterparties=["CPT-00201"] * 3,
    )


def layering_too_slow():
    """95% leaves to four counterparties, but over five days rather than two."""
    rows = [txn("E-R2d", 0, 9, 80_000.00, direction="inbound", channel="wire",
                counterparty="CPT-00999")]
    for i, amount in enumerate([22_000.0, 19_000.0, 18_000.0, 17_000.0]):
        rows.append(txn("E-R2d", 1 + i * 2, 9, amount, direction="outbound",
                        channel="wire", counterparty=f"CPT-{300 + i:05d}"))
    return build(rows)


def layering_inbound_too_small():
    """The dispersion pattern, on an amount below the rule's floor."""
    return _layering("E-R2e", 9_000.00, [2_500.0, 2_200.0, 2_100.0, 1_800.0])


# ---------------------------------------------------------------------------
# R3 -- smurfing
# ---------------------------------------------------------------------------
def smurfing_fires():
    """Twelve different senders, near-identical amounts, inside 72 hours."""
    return build([
        txn("E-R3", i // 4, 9 + (i % 4) * 3, 800.00 + (i % 3) * 20,
            direction="inbound", counterparty=f"CPT-{400 + i:05d}")
        for i in range(12)
    ])


def smurfing_too_few_senders():
    """Twelve payments in 72 hours, but from only three counterparties."""
    return build([
        txn("E-R3b", i // 4, 9 + (i % 4) * 3, 800.00 + (i % 3) * 20,
            direction="inbound", counterparty=f"CPT-{400 + (i % 3):05d}")
        for i in range(12)
    ])


def smurfing_amounts_too_varied():
    """Twelve distinct senders, but the amounts are nothing like each other."""
    amounts = [120.0, 4_800.0, 300.0, 2_900.0, 75.0, 6_100.0,
               210.0, 3_400.0, 90.0, 5_500.0, 150.0, 4_100.0]
    return build([
        txn("E-R3c", i // 4, 9 + (i % 4) * 3, amounts[i],
            direction="inbound", counterparty=f"CPT-{500 + i:05d}")
        for i in range(12)
    ])


def smurfing_too_spread_out():
    """Twelve distinct senders, similar amounts, but over three weeks."""
    return build([
        txn("E-R3d", i * 2, 10, 800.00 + (i % 3) * 20,
            direction="inbound", counterparty=f"CPT-{600 + i:05d}")
        for i in range(12)
    ])


# ---------------------------------------------------------------------------
# R4 -- round dollar
# ---------------------------------------------------------------------------
def round_dollar_fires():
    """A $5,000.00 payment from an entity that normally moves about $200."""
    return build(_history("E-R4") + [
        txn("E-R4", 20, 12, 5_000.00, direction="outbound", counterparty="CPT-00700")
    ])


def round_dollar_not_round():
    """Same outlier magnitude, but $5,137.42 -- not a round figure."""
    return build(_history("E-R4b") + [
        txn("E-R4b", 20, 12, 5_137.42, direction="outbound", counterparty="CPT-00700")
    ])


def round_dollar_not_anomalous():
    """An exact $1,000 multiple, from an entity for which that is normal."""
    return build(
        [txn("E-R4c", i, 10, 1_000.00 + (i % 4) * 25, direction="outbound",
             counterparty=f"CPT-{800 + i:05d}") for i in range(15)]
        + [txn("E-R4c", 20, 12, 1_000.00, direction="outbound", counterparty="CPT-00700")]
    )


def round_dollar_too_little_history():
    """A round outlier, but only four prior transactions to judge it against."""
    return build(_history("E-R4d", n=4) + [
        txn("E-R4d", 20, 12, 5_000.00, direction="outbound", counterparty="CPT-00700")
    ])


# ---------------------------------------------------------------------------
# R5 -- dormant reactivation
# ---------------------------------------------------------------------------
def dormant_fires():
    """Four months of silence, then eight times the usual amount."""
    return build(_history("E-R5", n=8) + [
        txn("E-R5", 128, 11, 4_000.00, direction="outbound", counterparty="CPT-00900")
    ])


def dormant_gap_too_short():
    """The same large transaction, after only a month of quiet."""
    return build(_history("E-R5b", n=8) + [
        txn("E-R5b", 38, 11, 4_000.00, direction="outbound", counterparty="CPT-00900")
    ])


def dormant_amount_too_small():
    """A long silence, broken by an entirely ordinary transaction."""
    return build(_history("E-R5c", n=8) + [
        txn("E-R5c", 128, 11, 240.00, direction="outbound", counterparty="CPT-00900")
    ])


# ---------------------------------------------------------------------------
# A quiet account that must trip nothing at all
# ---------------------------------------------------------------------------
def entirely_unremarkable():
    return build(_history("E-CLEAN", n=30, amount=340.0, step=7))


FIRES = {
    "R1_STRUCTURING": structuring_fires,
    "R2_LAYERING": layering_fires,
    "R3_SMURFING": smurfing_fires,
    "R4_ROUND_DOLLAR": round_dollar_fires,
    "R5_DORMANT": dormant_fires,
}

STAYS_SILENT = {
    "R1_STRUCTURING": {
        "only two deposits": structuring_too_few,
        "spread over 24 days": structuring_too_slow,
        "electronic, not cash": structuring_not_cash,
        "all above the threshold": structuring_above_band,
    },
    "R2_LAYERING": {
        "only 30% passed through": layering_retains_too_much,
        "single counterparty": layering_same_counterparty,
        "spread over five days": layering_too_slow,
        "inbound below the floor": layering_inbound_too_small,
    },
    "R3_SMURFING": {
        "only three senders": smurfing_too_few_senders,
        "amounts too varied": smurfing_amounts_too_varied,
        "spread over three weeks": smurfing_too_spread_out,
    },
    "R4_ROUND_DOLLAR": {
        "not a round figure": round_dollar_not_round,
        "round but unremarkable": round_dollar_not_anomalous,
        "too little history": round_dollar_too_little_history,
    },
    "R5_DORMANT": {
        "gap too short": dormant_gap_too_short,
        "amount too small": dormant_amount_too_small,
    },
}
