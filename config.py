"""
Single source of truth for every tunable in the simulation.

Nothing in this file is a real-world figure. The population sizes, behavioural
rates, typology parameters, rule thresholds and cost constants are all invented
for a synthetic teaching exercise. See the ILLUSTRATIVE COST MODEL block below
and the disclaimer at the top of README.md.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "generated"
SQL_DIR = ROOT / "sql"
SRC_DIR = ROOT / "src"
RESULTS_DIR = ROOT / "results"
DOCS_DIR = ROOT / "docs"

ENTITIES_PATH = DATA_DIR / "entities.csv.gz"
COUNTERPARTIES_PATH = DATA_DIR / "counterparties.csv.gz"
TRANSACTIONS_PATH = DATA_DIR / "transactions.csv.gz"
CASES_PATH = DATA_DIR / "cases.csv.gz"

# The SQLite database is a build artefact, not a source file -- it is rebuilt
# from the CSVs on every pipeline run and is gitignored.
DB_PATH = RESULTS_DIR / "aml_monitoring.db"

FEATURES_PATH = RESULTS_DIR / "features.csv.gz"
SCORED_PATH = RESULTS_DIR / "scored_transactions.csv.gz"
SWEEP_PATH = RESULTS_DIR / "threshold_sweep.csv"
METRICS_PATH = RESULTS_DIR / "metrics.json"

# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------
RANDOM_SEED = 29
CV_SEED = 4021
BOOTSTRAP_SEED = 7

N_CV_FOLDS = 5
BOOTSTRAP_ITERATIONS = 2000

# --------------------------------------------------------------------------
# Simulation window
# --------------------------------------------------------------------------
SIM_START = date(2025, 8, 1)
SIM_END = date(2026, 7, 31)

# --------------------------------------------------------------------------
# Population
# --------------------------------------------------------------------------
N_ENTITIES = 1_500
INDIVIDUAL_SHARE = 0.70
N_COUNTERPARTIES = 6_000

# Global multiplier on all baseline activity rates. Used to land the generated
# transaction count near TARGET_TRANSACTIONS without distorting the mix.
VOLUME_SCALE = 0.928
TARGET_TRANSACTIONS = 100_000

# The generator asserts it landed within this fraction of the target.
TRANSACTION_COUNT_TOLERANCE = 0.15

# Individual sub-segments -> share of individuals.
INDIVIDUAL_SEGMENTS = {
    "salaried": 0.60,
    "self_employed": 0.20,
    "retired": 0.10,
    "gig": 0.10,
}

# Business sub-segments -> share of businesses.
BUSINESS_SEGMENTS = {
    "retail_cash_intensive": 0.15,
    "professional_services": 0.35,
    "wholesale": 0.30,
    "ecommerce": 0.20,
}

# Counterparty pool composition. Employers and utilities are deliberately few
# and therefore high-degree: "this counterparty deals with many entities" must
# NOT be a signal on its own, or the network features are cheating.
COUNTERPARTY_MIX = {
    "employer": 0.020,
    "utility": 0.008,
    "landlord": 0.070,
    "merchant": 0.300,
    "supplier": 0.200,
    "individual": 0.395,
    "money_service": 0.007,
}

# Genericised geography -- no real place names anywhere in the data (C10).
REGIONS = ["Region A", "Region B", "Region C", "Region D", "Region E", "Region F"]

CHANNELS = ["cash_deposit", "wire", "eft", "etransfer", "cheque", "bill_payment"]

# --------------------------------------------------------------------------
# Typology injection
# --------------------------------------------------------------------------
# Share of entities that receive a planted typology. One typology per entity,
# so case attribution is never ambiguous.
TYPOLOGY_ENTITY_RATE = 0.04

TYPOLOGY_TOGGLES = {
    "structuring": True,
    "layering": True,
    "smurfing": True,
    "round_dollar": True,
    "dormant_reactivation": True,
}

# T1 -- structuring
STRUCTURING_MIN_DEPOSITS = 3
STRUCTURING_MAX_DEPOSITS = 6
STRUCTURING_AMOUNT_MIN = 8_000.00
STRUCTURING_AMOUNT_MAX = 9_999.99
STRUCTURING_FAST_WINDOW_DAYS = 7
# The stress subset: same deposits, spread beyond any 7-day rule window.
STRUCTURING_SLOW_SHARE = 0.25
STRUCTURING_SLOW_SPAN_MIN_DAYS = 9
STRUCTURING_SLOW_SPAN_MAX_DAYS = 14

# T2 -- rapid layering
LAYERING_LUMP_MIN = 25_000.00
LAYERING_LUMP_MAX = 150_000.00
LAYERING_MIN_LEGS = 3
LAYERING_MAX_LEGS = 6
LAYERING_RETENTION_MIN = 0.02
LAYERING_RETENTION_MAX = 0.10
LAYERING_MIN_LEG_SHARE = 0.05
LAYERING_WINDOW_MIN_HOURS = 4
LAYERING_WINDOW_MAX_HOURS = 36
# Share of layering cases whose outbound legs reuse a small shared pool of
# counterparties -- how these networks actually behave, and the reason the
# network features have anything to find.
LAYERING_MULE_POOL_SIZE = 30
LAYERING_MULE_POOL_SHARE = 0.40

# T3 -- smurfing
SMURFING_MIN_SENDERS = 10
SMURFING_MAX_SENDERS = 18
SMURFING_WINDOW_HOURS = 72
SMURFING_MEAN_MIN = 400.00
SMURFING_MEAN_MAX = 1_800.00
SMURFING_AMOUNT_CV = 0.12

# T4 -- round-dollar anomaly
ROUND_DOLLAR_MIN_TXNS = 4
ROUND_DOLLAR_MAX_TXNS = 8
ROUND_DOLLAR_MULTIPLE = 1_000.00
ROUND_DOLLAR_MIN_MULTIPLIER = 3.0
ROUND_DOLLAR_MAX_MULTIPLIER = 12.0
ROUND_DOLLAR_SPAN_MIN_DAYS = 30
ROUND_DOLLAR_SPAN_MAX_DAYS = 60

# T5 -- dormant reactivation
DORMANT_MIN_GAP_DAYS = 90
DORMANT_MAX_GAP_DAYS = 180
DORMANT_MIN_MULTIPLIER = 5.0
DORMANT_MAX_MULTIPLIER = 15.0
DORMANT_MIN_TXNS = 1
DORMANT_MAX_TXNS = 3

# --------------------------------------------------------------------------
# Feature layer
# --------------------------------------------------------------------------
WINDOW_7D_SECONDS = 7 * 24 * 3600
WINDOW_30D_SECONDS = 30 * 24 * 3600
WINDOW_48H_SECONDS = 48 * 3600

# Minimum prior transactions before an entity-relative z-score means anything.
ZSCORE_MIN_HISTORY = 5
ZSCORE_MIN_STDDEV = 1e-9

ROUND_AMOUNT_CENTS = 100_000  # $1,000.00 expressed in cents

OUT_IN_RATIO_CAP = 10.0
NEW_COUNTERPARTY_WINDOW_SECONDS = 7 * 24 * 3600

# The 15 features computed identically in sql/02_features.sql and
# src/features.py. The parity test iterates this list.
FEATURE_COLUMNS = [
    "txn_count_7d",
    "txn_sum_7d_cents",
    "txn_count_30d",
    "txn_sum_30d_cents",
    "amount_zscore_entity",
    "is_round_amount",
    "days_since_prev_txn",
    "distinct_counterparties_7d",
    "outbound_to_inbound_ratio_48h",
    "cash_ratio_7d",
    "counterparty_entity_degree_asof",
    "counterparty_age_days",
    "new_counterparty_ratio_7d",
    "hour_of_day",
    "is_weekend",
]

# Ground-truth columns. These must never reach the model feature matrix; a test
# asserts the intersection is empty.
GROUND_TRUTH_COLUMNS = ["is_suspicious", "typology", "typology_variant", "case_id"]

# --------------------------------------------------------------------------
# Rules engine (R1-R5)
# --------------------------------------------------------------------------
# Thresholds are deliberately LOOSER than the generator's parameters. A rule
# tuned to the generator is a fingerprint of the generator, and every metric
# derived from it would be theatre.

# R1 -- structuring. Generator plants $8,000.00-$9,999.99; the rule opens the
# floor to $7,500 and so also catches legitimate cash-intensive businesses.
R1_MIN_DEPOSITS = 3
R1_AMOUNT_FLOOR = 7_500.00
R1_AMOUNT_CEILING = 9_999.99
R1_WINDOW_DAYS = 7

# R2 -- layering. Generator plants >= $25,000 with 90-98% passed through.
R2_MIN_INBOUND = 15_000.00
R2_MIN_LEGS = 3
R2_MIN_DISTINCT_COUNTERPARTIES = 3
R2_MIN_PASSTHROUGH = 0.70
R2_WINDOW_HOURS = 48

# R3 -- smurfing. Generator plants 10-18 senders at CV ~0.12, mean <= $1,800.
R3_MIN_DISTINCT_SENDERS = 8
R3_MAX_AMOUNT_CV = 0.35
R3_MAX_MEAN_AMOUNT = 5_000.00
R3_WINDOW_HOURS = 72

# R4 -- round dollar. Generator plants 3-12x the entity median, above its p90.
R4_MIN_PRIOR_TXNS = 10
R4_MIN_ZSCORE = 2.0

# R5 -- dormant reactivation. Generator plants a 90-180 day gap then 5-15x.
R5_MIN_DORMANT_DAYS = 90
R5_MIN_MULTIPLIER = 4.0

RULE_CODES = ["R1_STRUCTURING", "R2_LAYERING", "R3_SMURFING", "R4_ROUND_DOLLAR", "R5_DORMANT"]

# --------------------------------------------------------------------------
# Anomaly model
# --------------------------------------------------------------------------
ISOLATION_FOREST_PARAMS = {
    "n_estimators": 300,
    "max_samples": 4096,
    "contamination": 0.02,
    "random_state": RANDOM_SEED,
    "n_jobs": -1,
}

LOF_PARAMS = {
    "n_neighbors": 35,
    "contamination": 0.02,
    "novelty": True,
    "n_jobs": -1,
}

SUPERVISED_PARAMS = {
    "max_iter": 2000,
    "class_weight": "balanced",
    "random_state": RANDOM_SEED,
}

# --------------------------------------------------------------------------
# Risk scorer
# --------------------------------------------------------------------------
RULE_WEIGHT = 0.60
MODEL_WEIGHT = 0.40
# Number of simultaneously firing rules at which the rule component saturates.
RULE_SATURATION = 2

THRESHOLD_SWEEP = list(range(0, 101))

# --------------------------------------------------------------------------
# ILLUSTRATIVE COST MODEL  (C9)
# --------------------------------------------------------------------------
# These four numbers are INVENTED for this simulation. They are not industry
# benchmarks, not regulatory figures, and not drawn from any real institution's
# operating data. They exist so the threshold sweep has a cost axis at all.
#
# Because the cost-optimal threshold is entirely a function of the ratio
# between them, the pipeline also reports a sensitivity sweep across
# COST_SENSITIVITY_MISS_VALUES. Quote the shape of the curve, not the dollar.
ANALYST_HOURLY_RATE_CAD = 85.00
INVESTIGATION_HOURS_PER_ALERT = 2.5
COST_PER_INVESTIGATION_CAD = ANALYST_HOURLY_RATE_CAD * INVESTIGATION_HOURS_PER_ALERT
PROXY_COST_PER_MISSED_CASE_CAD = 25_000.00

COST_SENSITIVITY_MISS_VALUES = [5_000.00, 25_000.00, 100_000.00]

# Analyst capacity, used for the alert-budget view in the dashboard. Also
# illustrative.
ANALYST_HEADCOUNT = 4
ANALYST_HOURS_PER_WEEK = 30.0

# --------------------------------------------------------------------------
# Naive baseline (C8)
# --------------------------------------------------------------------------
NAIVE_BASELINE_THRESHOLD_CAD = 10_000.00

# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
MASK_VISIBLE_CHARS = 4
DISCLAIMER = (
    "SYNTHETIC DEMONSTRATION ONLY - all entities, counterparties and "
    "transactions on this page were generated with Faker. This is a portfolio "
    "simulation of transaction-monitoring analytics, not a compliance product, "
    "and it contains no real institution's data."
)
