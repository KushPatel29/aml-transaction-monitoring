"""
Synthetic transaction generator.

Produces a monitored-payment population -- entities, counterparties and roughly
100,000 transactions over twelve simulated months -- and then plants five
suspicious-activity typologies into a small share of entities.

Everything here is fabricated. Names come from Faker, geography is genericised
to "Region A".."Region F", and no account numbers, national identifiers or
institution names exist anywhere in the output (C1, C10).

Scope note: the transaction set models *monitored payment activity* -- deposits,
transfers, wires, cheques, e-transfers and bill payments. Retail card spend is
deliberately out of scope, which is both how real monitoring systems are scoped
and the reason ~67 transactions per entity per year reads as realistic.

Run directly:  python data/generate_transactions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402

# ---------------------------------------------------------------------------
# Calendar scaffolding
#
# All timestamps are generated as integer epoch seconds derived from pandas
# Timestamps, never from datetime.timestamp(), which would silently bind the
# output to the generating machine's timezone and break CI reproducibility.
# ---------------------------------------------------------------------------
EPOCH0 = int(pd.Timestamp(cfg.SIM_START).value // 10**9)
SIM_DAYS = (cfg.SIM_END - cfg.SIM_START).days + 1

_CAL = pd.date_range(cfg.SIM_START, cfg.SIM_END, freq="D")
DAY_IS_WEEKEND = np.array([d.weekday() >= 5 for d in _CAL])
WEEKDAY_DAYS = np.where(~DAY_IS_WEEKEND)[0]

# Consumers transact on weekends, just less.
_CONSUMER_W = np.where(DAY_IS_WEEKEND, 0.75, 1.0)
CONSUMER_DAY_P = _CONSUMER_W / _CONSUMER_W.sum()

_TUE_THU = np.array([d.weekday() in (1, 3) for d in _CAL])
BATCH_DAYS = np.where(_TUE_THU)[0]


def _day_index(ts: pd.Timestamp) -> int | None:
    """Day offset from SIM_START, or None if the date falls outside the window."""
    idx = (ts.normalize() - pd.Timestamp(cfg.SIM_START)).days
    return int(idx) if 0 <= idx < SIM_DAYS else None


def _calendar_anchors() -> tuple[list[int], list[int], list[int]]:
    """Month-start, mid-month and last-business-day offsets inside the window."""
    firsts, fifteenths, last_business = [], [], []
    for period in pd.period_range(cfg.SIM_START, cfg.SIM_END, freq="M"):
        for target, bucket in (
            (period.start_time, firsts),
            (pd.Timestamp(year=period.year, month=period.month, day=15), fifteenths),
        ):
            idx = _day_index(target)
            if idx is not None:
                bucket.append(idx)
        end = period.end_time.normalize()
        while end.weekday() >= 5:
            end -= pd.Timedelta(days=1)
        idx = _day_index(end)
        if idx is not None:
            last_business.append(idx)
    return firsts, fifteenths, last_business


MONTH_FIRST, MONTH_FIFTEENTH, MONTH_LAST_BUSINESS = _calendar_anchors()
PAYROLL_DAYS = sorted(MONTH_FIFTEENTH + MONTH_LAST_BUSINESS)


def _hour_profile(weights: dict[int, float]) -> np.ndarray:
    profile = np.zeros(24)
    for hour, weight in weights.items():
        profile[hour] = weight
    return profile / profile.sum()


HOURS_PAYROLL = _hour_profile({7: 1, 8: 2, 9: 2, 10: 1})
HOURS_BUSINESS = _hour_profile({h: 1.0 for h in range(9, 18)})
HOURS_CONSUMER = _hour_profile(
    {8: 1, 9: 2, 10: 2, 11: 2, 12: 4, 13: 3, 14: 2, 15: 2, 16: 2,
     17: 3, 18: 4, 19: 4, 20: 3, 21: 2, 22: 1}
)
HOURS_BANKING = _hour_profile({h: 1.0 for h in range(9, 18)})

RAW_COLUMNS = [
    "entity_id",
    "counterparty_id",
    "txn_epoch",
    "amount_cents",
    "direction",
    "channel",
    "is_cash",
    "is_suspicious",
    "typology",
    "typology_variant",
    "case_id",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _epochs(rng: np.random.Generator, days: np.ndarray, hour_profile: np.ndarray) -> np.ndarray:
    """Turn day offsets into epoch seconds with a plausible time of day."""
    n = len(days)
    hours = rng.choice(24, size=n, p=hour_profile)
    return (
        EPOCH0
        + days.astype(np.int64) * 86_400
        + hours.astype(np.int64) * 3_600
        + rng.integers(0, 60, size=n) * 60
        + rng.integers(0, 60, size=n)
    )


def _lognormal_cents(rng: np.random.Generator, median: float, sigma: float, n: int,
                     low: float = 1.0, high: float = 5_000_000.0) -> np.ndarray:
    draws = rng.lognormal(mean=np.log(median), sigma=sigma, size=n)
    return np.round(np.clip(draws, low, high) * 100).astype(np.int64)


def _poisson_count(rng: np.random.Generator, annual_rate: float) -> int:
    return int(rng.poisson(annual_rate * cfg.VOLUME_SCALE))


def _emit(out: list, entity_id: str, cp_ids, epochs, amounts_cents,
          direction: str, channel: str, is_cash: int) -> None:
    cp_list = [None] * len(epochs) if cp_ids is None else cp_ids
    for cp, epoch, amount in zip(cp_list, epochs, amounts_cents):
        out.append(
            (entity_id, cp, int(epoch), int(amount), direction, channel,
             is_cash, 0, None, None, None)
        )


def _snap_to_multiple(cents: np.ndarray, multiple_dollars: int) -> np.ndarray:
    """Snap to the nearest multiple, never below one full multiple.

    Without the floor, an invoice under half a step rounds to zero -- which is
    how fifteen $0.00 supplier payments got into the first generated run.
    """
    step = multiple_dollars * 100
    return np.maximum(np.round(cents / step) * step, step).astype(np.int64)


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------
def build_entities(rng: np.random.Generator, fake: Faker) -> pd.DataFrame:
    n_individual = int(round(cfg.N_ENTITIES * cfg.INDIVIDUAL_SHARE))
    n_business = cfg.N_ENTITIES - n_individual

    def _segments(mapping: dict[str, float], n: int) -> np.ndarray:
        names = list(mapping)
        probs = np.array([mapping[k] for k in names], dtype=float)
        return rng.choice(names, size=n, p=probs / probs.sum())

    records = []
    for i in range(n_individual):
        records.append(
            {
                "entity_id": f"ENT-{i + 1:05d}",
                "entity_type": "individual",
                "display_name": fake.name(),
            }
        )
    for j in range(n_business):
        i = n_individual + j
        records.append(
            {
                "entity_id": f"ENT-{i + 1:05d}",
                "entity_type": "business",
                # Numeric suffix makes accidental collision with a real
                # company name vanishingly unlikely and signals "synthetic".
                "display_name": f"{fake.company()} {j + 1:04d}",
            }
        )

    entities = pd.DataFrame(records)
    entities["segment"] = ""
    is_ind = entities["entity_type"] == "individual"
    entities.loc[is_ind, "segment"] = _segments(cfg.INDIVIDUAL_SEGMENTS, n_individual)
    entities.loc[~is_ind, "segment"] = _segments(cfg.BUSINESS_SEGMENTS, n_business)

    entities["home_region"] = rng.choice(cfg.REGIONS, size=len(entities))
    # Accounts opened between five years and one month before the window.
    entities["opened_days_before_sim"] = rng.integers(30, 1826, size=len(entities))
    entities["opened_date"] = [
        (pd.Timestamp(cfg.SIM_START) - pd.Timedelta(days=int(d))).date().isoformat()
        for d in entities["opened_days_before_sim"]
    ]

    # Per-entity behavioural parameters.
    entities["payroll_base_cad"] = np.where(
        is_ind, np.round(rng.lognormal(np.log(2_200), 0.35, len(entities)), 2), 0.0
    )
    entities["rent_cad"] = np.where(
        is_ind, np.round(rng.uniform(900, 3_400, len(entities)) / 25) * 25, 0.0
    )
    entities["typical_amount_cad"] = np.where(
        is_ind,
        np.round(rng.lognormal(np.log(180), 0.55, len(entities)), 2),
        np.round(rng.lognormal(np.log(2_400), 0.65, len(entities)), 2),
    )
    return entities.drop(columns=["opened_days_before_sim"])


def build_counterparties(rng: np.random.Generator, fake: Faker) -> pd.DataFrame:
    records = []
    idx = 0
    for cp_type, share in cfg.COUNTERPARTY_MIX.items():
        count = int(round(cfg.N_COUNTERPARTIES * share))
        for _ in range(count):
            idx += 1
            if cp_type in ("individual",):
                name = fake.name()
            else:
                name = f"{fake.company()} {idx:05d}"
            records.append(
                {
                    "counterparty_id": f"CPT-{idx:05d}",
                    "counterparty_type": cp_type,
                    "display_name": name,
                    "home_region": rng.choice(cfg.REGIONS),
                }
            )
    return pd.DataFrame(records)


def assign_wallets(entities: pd.DataFrame, counterparties: pd.DataFrame,
                   rng: np.random.Generator) -> dict[str, dict[str, np.ndarray]]:
    """
    Give every entity a stable set of counterparties it deals with.

    Selection is preferential-attachment weighted, so employers and utilities
    become genuine high-degree hubs. That matters: if the only high-degree
    counterparties in the data were the planted mules, the network features
    would be detecting the generator rather than the behaviour.
    """
    pools: dict[str, np.ndarray] = {}
    weights: dict[str, np.ndarray] = {}
    for cp_type, group in counterparties.groupby("counterparty_type"):
        ids = group["counterparty_id"].to_numpy()
        pools[cp_type] = ids
        w = rng.pareto(1.3, size=len(ids)) + 1.0
        weights[cp_type] = w / w.sum()

    def pick(cp_type: str, n: int) -> np.ndarray:
        n = min(n, len(pools[cp_type]))
        return rng.choice(pools[cp_type], size=n, replace=False, p=weights[cp_type])

    wallets: dict[str, dict[str, np.ndarray]] = {}
    for row in entities.itertuples(index=False):
        if row.entity_type == "individual":
            wallets[row.entity_id] = {
                "employer": pick("employer", 1),
                "landlord": pick("landlord", 1),
                "utility": pick("utility", int(rng.integers(1, 3))),
                "merchant": pick("merchant", int(rng.integers(6, 16))),
                "peer": pick("individual", int(rng.integers(2, 7))),
            }
        else:
            wallets[row.entity_id] = {
                "supplier": pick("supplier", int(rng.integers(5, 21))),
                "payroll_provider": pick("supplier", 1),
                "customer": np.concatenate(
                    [
                        pick("merchant", int(rng.integers(4, 13))),
                        pick("individual", int(rng.integers(8, 31))),
                    ]
                ),
                "utility": pick("utility", int(rng.integers(1, 3))),
                "landlord": pick("landlord", 1),
            }
    return wallets


# ---------------------------------------------------------------------------
# Baseline (legitimate) activity
# ---------------------------------------------------------------------------
def _gen_individual(rng, ent, wallet, out) -> None:
    eid = ent.entity_id
    seg = ent.segment
    merchants = wallet["merchant"]
    peers = wallet["peer"]

    if seg in ("salaried", "retired"):
        days = np.array(PAYROLL_DAYS if seg == "salaried" else MONTH_FIRST)
        base = ent.payroll_base_cad if seg == "salaried" else ent.payroll_base_cad * 0.65
        amounts = np.round(base * rng.normal(1.0, 0.02, len(days)) * 100).astype(np.int64)
        _emit(out, eid, [wallet["employer"][0]] * len(days),
              _epochs(rng, days, HOURS_PAYROLL), amounts, "inbound", "eft", 0)

        rent_days = np.array(MONTH_FIRST) + rng.integers(0, 2, len(MONTH_FIRST))
        rent_days = np.clip(rent_days, 0, SIM_DAYS - 1)
        rent_cents = np.full(len(rent_days), int(round(ent.rent_cad * 100)), dtype=np.int64)
        _emit(out, eid, [wallet["landlord"][0]] * len(rent_days),
              _epochs(rng, rent_days, HOURS_CONSUMER), rent_cents,
              "outbound", "bill_payment", 0)

        n_disc = _poisson_count(rng, 12 if seg == "salaried" else 20)
        if n_disc:
            days = rng.choice(SIM_DAYS, size=n_disc, p=CONSUMER_DAY_P)
            amounts = _lognormal_cents(rng, ent.typical_amount_cad, 0.9, n_disc)
            cps = rng.choice(np.concatenate([merchants, peers]), size=n_disc)
            channels = rng.choice(["etransfer", "bill_payment", "cheque"],
                                  size=n_disc, p=[0.6, 0.3, 0.1])
            for cp, epoch, amount, channel in zip(
                cps, _epochs(rng, days, HOURS_CONSUMER), amounts, channels
            ):
                out.append((eid, cp, int(epoch), int(amount), "outbound",
                            str(channel), 0, 0, None, None, None))

    elif seg == "self_employed":
        n_in = _poisson_count(rng, 30)
        if n_in:
            days = rng.choice(WEEKDAY_DAYS, size=n_in)
            _emit(out, eid, rng.choice(merchants, size=n_in),
                  _epochs(rng, days, HOURS_BUSINESS),
                  _lognormal_cents(rng, 1_500, 0.9, n_in), "inbound", "eft", 0)
        n_out = _poisson_count(rng, 15)
        if n_out:
            days = rng.choice(SIM_DAYS, size=n_out, p=CONSUMER_DAY_P)
            _emit(out, eid, rng.choice(np.concatenate([merchants, peers]), size=n_out),
                  _epochs(rng, days, HOURS_CONSUMER),
                  _lognormal_cents(rng, ent.typical_amount_cad, 0.85, n_out),
                  "outbound", "etransfer", 0)
        n_cash = _poisson_count(rng, 3)
        if n_cash:
            days = rng.choice(WEEKDAY_DAYS, size=n_cash)
            _emit(out, eid, None, _epochs(rng, days, HOURS_BANKING),
                  _lognormal_cents(rng, 900, 0.7, n_cash), "inbound", "cash_deposit", 1)

    else:  # gig
        n_in = _poisson_count(rng, 60)
        if n_in:
            days = rng.choice(SIM_DAYS, size=n_in, p=CONSUMER_DAY_P)
            _emit(out, eid, rng.choice(merchants, size=n_in),
                  _epochs(rng, days, HOURS_CONSUMER),
                  _lognormal_cents(rng, 120, 0.7, n_in), "inbound", "etransfer", 0)
        n_out = _poisson_count(rng, 20)
        if n_out:
            days = rng.choice(SIM_DAYS, size=n_out, p=CONSUMER_DAY_P)
            _emit(out, eid, rng.choice(np.concatenate([merchants, peers]), size=n_out),
                  _epochs(rng, days, HOURS_CONSUMER),
                  _lognormal_cents(rng, ent.typical_amount_cad, 0.9, n_out),
                  "outbound", "etransfer", 0)


def _gen_business(rng, ent, wallet, out) -> None:
    eid = ent.entity_id
    seg = ent.segment
    suppliers = wallet["supplier"]
    customers = wallet["customer"]

    # Aggregated payroll run -- one debit per pay period to a payroll provider.
    payroll_days = np.array(PAYROLL_DAYS if seg != "ecommerce" else MONTH_LAST_BUSINESS)
    payroll_cents = _lognormal_cents(rng, 12_000, 0.5, len(payroll_days))
    _emit(out, eid, [wallet["payroll_provider"][0]] * len(payroll_days),
          _epochs(rng, payroll_days, HOURS_BUSINESS), payroll_cents,
          "outbound", "eft", 0)

    if seg == "retail_cash_intensive":
        n_cash = _poisson_count(rng, 78)
        if n_cash:
            days = rng.choice(WEEKDAY_DAYS, size=n_cash)
            amounts = _lognormal_cents(rng, 4_200, 0.55, n_cash, low=500, high=14_000)
            _emit(out, eid, None, _epochs(rng, days, HOURS_BANKING), amounts,
                  "inbound", "cash_deposit", 1)
        n_sup = _poisson_count(rng, 30)
    elif seg == "professional_services":
        n_in = _poisson_count(rng, 60)
        if n_in:
            days = rng.choice(WEEKDAY_DAYS, size=n_in)
            _emit(out, eid, rng.choice(customers, size=n_in),
                  _epochs(rng, days, HOURS_BUSINESS),
                  _lognormal_cents(rng, 3_200, 1.0, n_in), "inbound", "eft", 0)
        n_sup = _poisson_count(rng, 20)
    elif seg == "wholesale":
        n_in = _poisson_count(rng, 50)
        if n_in:
            days = rng.choice(WEEKDAY_DAYS, size=n_in)
            channels = rng.choice(["eft", "wire", "cheque"], size=n_in, p=[0.6, 0.2, 0.2])
            for cp, epoch, amount, channel in zip(
                rng.choice(customers, size=n_in),
                _epochs(rng, days, HOURS_BUSINESS),
                _lognormal_cents(rng, 5_800, 1.0, n_in),
                channels,
            ):
                out.append((eid, cp, int(epoch), int(amount), "inbound",
                            str(channel), 0, 0, None, None, None))
        # Supplier payments arrive in batches: several within the same hour.
        n_batches = _poisson_count(rng, 8)
        for _ in range(n_batches):
            size = int(rng.integers(3, 9))
            day = int(rng.choice(BATCH_DAYS))
            base = _epochs(rng, np.array([day]), HOURS_BUSINESS)[0]
            epochs = base + rng.integers(0, 3_600, size=size)
            _emit(out, eid, rng.choice(suppliers, size=size), epochs,
                  _lognormal_cents(rng, 1_800, 1.0, size), "outbound", "eft", 0)
        n_sup = 0
    else:  # ecommerce
        n_in = _poisson_count(rng, 80)
        if n_in:
            days = rng.choice(SIM_DAYS, size=n_in, p=CONSUMER_DAY_P)
            _emit(out, eid, rng.choice(customers, size=n_in),
                  _epochs(rng, days, HOURS_BUSINESS),
                  _lognormal_cents(rng, 1_500, 0.8, n_in), "inbound", "eft", 0)
        n_sup = _poisson_count(rng, 20)

    if n_sup:
        days = rng.choice(WEEKDAY_DAYS, size=n_sup)
        amounts = _lognormal_cents(rng, 1_800, 1.0, n_sup)
        # Some invoices settle at round figures. This is legitimate round-dollar
        # noise, and it is what stops R4 from being a free win.
        round_mask = rng.random(n_sup) < 0.08
        amounts[round_mask] = _snap_to_multiple(amounts[round_mask], 500)
        channels = rng.choice(["eft", "cheque", "wire"], size=n_sup, p=[0.65, 0.25, 0.10])
        for cp, epoch, amount, channel in zip(
            rng.choice(suppliers, size=n_sup),
            _epochs(rng, days, HOURS_BUSINESS),
            amounts,
            channels,
        ):
            out.append((eid, cp, int(epoch), int(amount), "outbound",
                        str(channel), 0, 0, None, None, None))


def generate_baseline(entities: pd.DataFrame, wallets: dict,
                      rng: np.random.Generator) -> pd.DataFrame:
    out: list[tuple] = []
    for ent in entities.itertuples(index=False):
        wallet = wallets[ent.entity_id]
        if ent.entity_type == "individual":
            _gen_individual(rng, ent, wallet, out)
        else:
            _gen_business(rng, ent, wallet, out)
    return pd.DataFrame(out, columns=RAW_COLUMNS)


# ---------------------------------------------------------------------------
# Typology injection
# ---------------------------------------------------------------------------
def _case_rows(rows: list[tuple], typology: str, variant: str | None,
               case_id: str) -> list[tuple]:
    """Stamp ground truth onto a case's transactions."""
    return [r[:7] + (1, typology, variant, case_id) for r in rows]


def _rand_start_day(rng, span_days: int, margin: int = 20) -> int:
    latest = SIM_DAYS - span_days - margin
    return int(rng.integers(margin, max(margin + 1, latest)))


def inject_structuring(rng, eid, wallet, hist, case_id) -> tuple[list, dict]:
    slow = rng.random() < cfg.STRUCTURING_SLOW_SHARE
    n = int(rng.integers(cfg.STRUCTURING_MIN_DEPOSITS, cfg.STRUCTURING_MAX_DEPOSITS + 1))

    if slow:
        span = int(rng.integers(cfg.STRUCTURING_SLOW_SPAN_MIN_DAYS,
                                cfg.STRUCTURING_SLOW_SPAN_MAX_DAYS + 1))
        # Pin the first and last deposit to the ends so the span is guaranteed
        # to exceed any 7-day rule window.
        middle = rng.choice(np.arange(1, span), size=n - 2, replace=False) if n > 2 else []
        offsets = np.sort(np.concatenate([[0], middle, [span]])).astype(int)
        variant = "slow"
    else:
        span = cfg.STRUCTURING_FAST_WINDOW_DAYS - 1
        offsets = np.sort(rng.choice(span + 1, size=n, replace=False)).astype(int)
        variant = "fast"

    start = _rand_start_day(rng, span + 2)
    days = np.clip(start + offsets, 0, SIM_DAYS - 1)
    amounts = np.round(
        rng.uniform(cfg.STRUCTURING_AMOUNT_MIN, cfg.STRUCTURING_AMOUNT_MAX, n) * 100
    ).astype(np.int64)
    epochs = np.sort(_epochs(rng, days, HOURS_BANKING))

    rows = [
        (eid, None, int(e), int(a), "inbound", "cash_deposit", 1, 0, None, None, None)
        for e, a in zip(epochs, amounts)
    ]
    return _case_rows(rows, "structuring", variant, case_id), {"variant": variant}


def inject_layering(rng, eid, wallet, hist, case_id, mule_pool) -> tuple[list, dict]:
    lump = rng.uniform(cfg.LAYERING_LUMP_MIN, cfg.LAYERING_LUMP_MAX)
    lump_cents = int(round(lump * 100))
    k = int(rng.integers(cfg.LAYERING_MIN_LEGS, cfg.LAYERING_MAX_LEGS + 1))
    retention = rng.uniform(cfg.LAYERING_RETENTION_MIN, cfg.LAYERING_RETENTION_MAX)

    # Split the pass-through across k legs with a guaranteed floor per leg.
    floor = cfg.LAYERING_MIN_LEG_SHARE
    weights = floor + rng.dirichlet(np.full(k, 4.0)) * (1.0 - floor * k)
    out_total = int(round(lump_cents * (1.0 - retention)))
    leg_cents = np.round(weights * out_total).astype(np.int64)
    leg_cents[-1] = out_total - leg_cents[:-1].sum()

    start_day = _rand_start_day(rng, 4)
    t0 = int(_epochs(rng, np.array([start_day]), HOURS_BUSINESS)[0])
    offsets = np.sort(
        rng.integers(cfg.LAYERING_WINDOW_MIN_HOURS * 3_600,
                     cfg.LAYERING_WINDOW_MAX_HOURS * 3_600, size=k)
    )

    use_pool = rng.random() < cfg.LAYERING_MULE_POOL_SHARE
    source = mule_pool if use_pool else wallet.get("supplier", wallet.get("merchant"))
    legs_cp = rng.choice(source, size=min(k, len(source)), replace=False)
    while len(legs_cp) < k:  # pool smaller than k -- pad from the wider set
        legs_cp = np.concatenate([legs_cp, rng.choice(mule_pool, size=k - len(legs_cp))])

    inbound_cp = rng.choice(mule_pool if use_pool else source)
    rows = [(eid, inbound_cp, t0, lump_cents, "inbound", "wire", 0, 0, None, None, None)]
    channels = rng.choice(["wire", "eft", "etransfer"], size=k, p=[0.4, 0.4, 0.2])
    for cp, offset, amount, channel in zip(legs_cp, offsets, leg_cents, channels):
        rows.append((eid, cp, int(t0 + offset), int(amount), "outbound",
                     str(channel), 0, 0, None, None, None))

    return _case_rows(rows, "layering", "mule_pool" if use_pool else "dispersed", case_id), {
        "retention": retention,
        "used_mule_pool": bool(use_pool),
    }


def inject_smurfing(rng, eid, wallet, hist, case_id, sender_pool) -> tuple[list, dict]:
    m = int(rng.integers(cfg.SMURFING_MIN_SENDERS, cfg.SMURFING_MAX_SENDERS + 1))
    mean = rng.uniform(cfg.SMURFING_MEAN_MIN, cfg.SMURFING_MEAN_MAX)
    amounts = np.round(
        np.clip(rng.normal(mean, cfg.SMURFING_AMOUNT_CV * mean, m), 50, None) * 100
    ).astype(np.int64)

    start_day = _rand_start_day(rng, 5)
    t0 = int(_epochs(rng, np.array([start_day]), HOURS_CONSUMER)[0])
    offsets = np.sort(rng.integers(0, cfg.SMURFING_WINDOW_HOURS * 3_600, size=m))
    senders = rng.choice(sender_pool, size=m, replace=False)

    rows = [
        (eid, cp, int(t0 + off), int(amt), "inbound", "etransfer", 0, 0, None, None, None)
        for cp, off, amt in zip(senders, offsets, amounts)
    ]
    return _case_rows(rows, "smurfing", None, case_id), {"n_senders": m}


def inject_round_dollar(rng, eid, wallet, hist, case_id) -> tuple[list, dict]:
    amounts_cad = hist["amounts_cents"] / 100.0
    median = float(np.median(amounts_cad))
    p90 = float(np.percentile(amounts_cad, 90))

    j = int(rng.integers(cfg.ROUND_DOLLAR_MIN_TXNS, cfg.ROUND_DOLLAR_MAX_TXNS + 1))
    multipliers = rng.uniform(cfg.ROUND_DOLLAR_MIN_MULTIPLIER, cfg.ROUND_DOLLAR_MAX_MULTIPLIER, j)
    raw = np.maximum(median * multipliers, p90 * 1.05)
    # Exact multiples of $1,000, and never below one full multiple. Rounding up
    # rather than to nearest matters: np.round can land a case *below* the
    # entity's own p90, which would contradict the typology's definition.
    amounts = np.maximum(
        np.ceil(raw / cfg.ROUND_DOLLAR_MULTIPLE) * cfg.ROUND_DOLLAR_MULTIPLE,
        cfg.ROUND_DOLLAR_MULTIPLE,
    )
    amounts_cents = np.round(amounts * 100).astype(np.int64)

    span = int(rng.integers(cfg.ROUND_DOLLAR_SPAN_MIN_DAYS, cfg.ROUND_DOLLAR_SPAN_MAX_DAYS + 1))
    start = _rand_start_day(rng, span + 2)
    days = np.clip(start + np.sort(rng.choice(span, size=j, replace=False)), 0, SIM_DAYS - 1)

    pool = wallet.get("supplier", wallet.get("merchant"))
    rows = [
        (eid, cp, int(e), int(a), "outbound", "eft", 0, 0, None, None, None)
        for cp, e, a in zip(
            rng.choice(pool, size=j),
            np.sort(_epochs(rng, days, HOURS_BUSINESS)),
            amounts_cents,
        )
    ]
    return _case_rows(rows, "round_dollar", None, case_id), {"median_cad": median}


def inject_dormant_reactivation(rng, eid, wallet, hist, case_id) -> tuple[list, dict]:
    gap = int(rng.integers(cfg.DORMANT_MIN_GAP_DAYS, cfg.DORMANT_MAX_GAP_DAYS + 1))
    # Leave at least 60 days of history in front of the silence so there is
    # something to be dormant relative to.
    start = int(rng.integers(60, max(61, SIM_DAYS - gap - 10)))
    silence_start = EPOCH0 + start * 86_400
    planned_end = silence_start + gap * 86_400

    prior = hist["amounts_cents"][hist["epochs"] < silence_start]
    if len(prior) == 0:
        prior = hist["amounts_cents"]
    baseline_mean = float(prior.mean())

    n = int(rng.integers(cfg.DORMANT_MIN_TXNS, cfg.DORMANT_MAX_TXNS + 1))
    multipliers = rng.uniform(cfg.DORMANT_MIN_MULTIPLIER, cfg.DORMANT_MAX_MULTIPLIER, n)
    amounts = np.round(baseline_mean * multipliers).astype(np.int64)

    day = int((planned_end - EPOCH0) // 86_400)
    days = np.clip(day + np.arange(n), 0, SIM_DAYS - 1)
    epochs = np.sort(_epochs(rng, days, HOURS_BANKING))
    epochs = np.maximum(epochs, planned_end + 3_600)

    pool = wallet.get("supplier", wallet.get("merchant"))
    directions = rng.choice(["outbound", "inbound"], size=n, p=[0.6, 0.4])
    rows = [
        (eid, cp, int(e), int(a), str(d), "wire", 0, 0, None, None, None)
        for cp, e, a, d in zip(rng.choice(pool, size=n), epochs, amounts, directions)
    ]
    return _case_rows(rows, "dormant_reactivation", None, case_id), {
        "silence_start": int(silence_start),
        # Silence runs right up to the first reactivation, not to the nominal
        # end of the gap. Otherwise a legitimate transaction landing in the
        # hours between them collapses the observed dormancy to zero days.
        "silence_end": int(epochs.min()),
        "gap_days": gap,
        "baseline_mean_cad": baseline_mean / 100.0,
    }


def inject_typologies(txns: pd.DataFrame, entities: pd.DataFrame,
                      counterparties: pd.DataFrame, wallets: dict,
                      rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    active = [name for name, on in cfg.TYPOLOGY_TOGGLES.items() if on]
    if not active:
        return txns, pd.DataFrame(columns=["case_id", "entity_id", "typology"])

    n_cases = int(round(len(entities) * cfg.TYPOLOGY_ENTITY_RATE))
    # One typology per entity: attribution stays unambiguous.
    chosen = rng.choice(entities["entity_id"].to_numpy(), size=n_cases, replace=False)
    assigned = np.array([active[i % len(active)] for i in range(n_cases)])
    rng.shuffle(assigned)

    cp_by_type = counterparties.groupby("counterparty_type")["counterparty_id"]
    individual_cps = cp_by_type.get_group("individual").to_numpy()
    money_service_cps = cp_by_type.get_group("money_service").to_numpy()
    mule_candidates = np.concatenate([individual_cps, money_service_cps])
    mule_pool = rng.choice(mule_candidates, size=cfg.LAYERING_MULE_POOL_SIZE, replace=False)

    grouped = {
        eid: {"epochs": g["txn_epoch"].to_numpy(), "amounts_cents": g["amount_cents"].to_numpy()}
        for eid, g in txns.groupby("entity_id", sort=False)
    }

    new_rows: list[tuple] = []
    drop_mask = np.zeros(len(txns), dtype=bool)
    entity_positions = {eid: g.index.to_numpy() for eid, g in txns.groupby("entity_id", sort=False)}
    case_records = []

    for i, (eid, typology) in enumerate(zip(chosen, assigned)):
        case_id = f"CASE-{i + 1:04d}"
        wallet = wallets[eid]
        hist = grouped.get(eid, {"epochs": np.array([]), "amounts_cents": np.array([1_000])})
        if len(hist["amounts_cents"]) == 0:
            hist = {"epochs": np.array([EPOCH0]), "amounts_cents": np.array([100_000])}

        if typology == "structuring":
            rows, meta = inject_structuring(rng, eid, wallet, hist, case_id)
        elif typology == "layering":
            rows, meta = inject_layering(rng, eid, wallet, hist, case_id, mule_pool)
        elif typology == "smurfing":
            rows, meta = inject_smurfing(rng, eid, wallet, hist, case_id, individual_cps)
        elif typology == "round_dollar":
            rows, meta = inject_round_dollar(rng, eid, wallet, hist, case_id)
        else:
            rows, meta = inject_dormant_reactivation(rng, eid, wallet, hist, case_id)
            # Dormancy is created by removing baseline activity, not by adding.
            positions = entity_positions.get(eid, np.array([], dtype=int))
            if len(positions):
                epochs = txns["txn_epoch"].to_numpy()[positions]
                silenced = positions[
                    (epochs >= meta["silence_start"]) & (epochs < meta["silence_end"])
                ]
                drop_mask[silenced] = True

        new_rows.extend(rows)
        amounts = [r[3] for r in rows]
        epochs = [r[2] for r in rows]
        # Round the per-typology metadata. A raw float renders as seventeen
        # significant digits in CSV, which is both unreadable and an unbroken
        # digit run long enough to trip the account-number checks in
        # tests/test_no_pii_in_data.py.
        details = {
            k: round(v, 4) if isinstance(v, float) else v
            for k, v in meta.items()
            if k not in ("silence_start", "silence_end")
        }
        case_records.append(
            {
                "case_id": case_id,
                "entity_id": eid,
                "typology": typology,
                "typology_variant": rows[0][9],
                "n_transactions": len(rows),
                "total_amount_cad": round(sum(amounts) / 100.0, 2),
                "first_txn_epoch": min(epochs),
                "last_txn_epoch": max(epochs),
                "span_days": round((max(epochs) - min(epochs)) / 86_400.0, 2),
                **details,
            }
        )

    kept = txns.loc[~drop_mask].copy()
    combined = pd.concat([kept, pd.DataFrame(new_rows, columns=RAW_COLUMNS)], ignore_index=True)
    return combined, pd.DataFrame(case_records)


# ---------------------------------------------------------------------------
# Finalisation
# ---------------------------------------------------------------------------
def finalise(txns: pd.DataFrame) -> pd.DataFrame:
    txns = txns.sort_values(
        ["txn_epoch", "entity_id", "amount_cents", "direction", "channel"],
        kind="stable",
    ).reset_index(drop=True)
    txns.insert(0, "transaction_id", [f"TXN-{i + 1:07d}" for i in range(len(txns))])
    txns["txn_datetime"] = pd.to_datetime(txns["txn_epoch"], unit="s").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    txns["amount_cad"] = (txns["amount_cents"] / 100.0).round(2)
    txns["counterparty_id"] = txns["counterparty_id"].fillna("")
    txns["typology"] = txns["typology"].fillna("")
    txns["typology_variant"] = txns["typology_variant"].fillna("")
    txns["case_id"] = txns["case_id"].fillna("")
    return txns[
        [
            "transaction_id",
            "entity_id",
            "counterparty_id",
            "txn_datetime",
            "txn_epoch",
            "amount_cents",
            "amount_cad",
            "direction",
            "channel",
            "is_cash",
            "is_suspicious",
            "typology",
            "typology_variant",
            "case_id",
        ]
    ]


def main() -> pd.DataFrame:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(cfg.RANDOM_SEED)
    fake = Faker("en_CA")
    Faker.seed(cfg.RANDOM_SEED)

    print(f"Generating {cfg.N_ENTITIES} entities and {cfg.N_COUNTERPARTIES} counterparties...")
    entities = build_entities(rng, fake)
    counterparties = build_counterparties(rng, fake)
    wallets = assign_wallets(entities, counterparties, rng)

    print("Generating baseline activity...")
    txns = generate_baseline(entities, wallets, rng)
    baseline_rows = len(txns)

    print("Injecting typologies...")
    txns, cases = inject_typologies(txns, entities, counterparties, wallets, rng)
    txns = finalise(txns)

    entities.to_csv(cfg.ENTITIES_PATH, index=False, compression=cfg.GZIP)
    counterparties.to_csv(cfg.COUNTERPARTIES_PATH, index=False, compression=cfg.GZIP)
    txns.to_csv(cfg.TRANSACTIONS_PATH, index=False, compression=cfg.GZIP)
    cases.to_csv(cfg.CASES_PATH, index=False, compression=cfg.GZIP)

    suspicious = int(txns["is_suspicious"].sum())
    print(
        f"\n  baseline transactions : {baseline_rows:,}\n"
        f"  final transactions    : {len(txns):,}\n"
        f"  planted cases         : {len(cases)}\n"
        f"  suspicious rows       : {suspicious:,} "
        f"({suspicious / len(txns) * 100:.2f}% of transactions)\n"
        f"  entities with a case  : {cases['entity_id'].nunique()} "
        f"({cases['entity_id'].nunique() / len(entities) * 100:.1f}%)"
    )
    print("\n  by typology:")
    for typology, group in cases.groupby("typology"):
        variants = group["typology_variant"].replace("", "-").value_counts().to_dict()
        print(f"    {typology:<22} {len(group):>3} cases   variants={variants}")

    drift = abs(len(txns) - cfg.TARGET_TRANSACTIONS) / cfg.TARGET_TRANSACTIONS
    if drift > cfg.TRANSACTION_COUNT_TOLERANCE:
        raise SystemExit(
            f"Generated {len(txns):,} transactions, more than "
            f"{cfg.TRANSACTION_COUNT_TOLERANCE:.0%} from the "
            f"{cfg.TARGET_TRANSACTIONS:,} target. Adjust config.VOLUME_SCALE."
        )
    return txns


if __name__ == "__main__":
    main()
