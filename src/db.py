"""
SQLite warehouse: load the generated CSVs, then execute sql/ verbatim.

"Verbatim" is the point. The .sql files are read off disk and handed to SQLite
untouched -- no string formatting, no query builder, no parameter injection --
so the SQL a reviewer reads in sql/02_features.sql is exactly the SQL that
produced results/features.csv.gz.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

import pandas as pd

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)

import config as cfg  # noqa: E402

SCHEMA_SQL = cfg.SQL_DIR / "01_schema.sql"
FEATURES_SQL = cfg.SQL_DIR / "02_features.sql"

TRANSACTION_COLUMNS = [
    "transaction_id", "entity_id", "counterparty_id", "txn_datetime", "txn_epoch",
    "amount_cents", "direction", "channel", "is_cash", "is_suspicious",
    "typology", "typology_variant", "case_id",
]
ENTITY_COLUMNS = [
    "entity_id", "entity_type", "display_name", "segment", "home_region",
    "opened_date", "payroll_base_cad", "rent_cad", "typical_amount_cad",
]
COUNTERPARTY_COLUMNS = [
    "counterparty_id", "counterparty_type", "display_name", "home_region",
]


def _sqrt(value):
    if value is None or value < 0:
        return None
    return math.sqrt(value)


def _ensure_math_functions(conn: sqlite3.Connection) -> None:
    """
    SQLite only exposes sqrt() when built with SQLITE_ENABLE_MATH_FUNCTIONS.
    That is on for the interpreters this project is tested against, but it is a
    build-time flag rather than a version guarantee, so rather than let
    02_features.sql explode on some other machine we detect and backfill it.
    The SQL is unchanged either way.
    """
    try:
        conn.execute("SELECT sqrt(4.0)").fetchone()
    except sqlite3.OperationalError:
        conn.create_function("sqrt", 1, _sqrt, deterministic=True)


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or cfg.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_math_functions(conn)
    return conn


def run_sql_file(conn: sqlite3.Connection, path: Path) -> float:
    """Execute a .sql file exactly as committed. Returns elapsed seconds."""
    started = time.perf_counter()
    conn.executescript(path.read_text(encoding="utf-8"))
    conn.commit()
    return time.perf_counter() - started


def load_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    transactions = pd.read_csv(cfg.TRANSACTIONS_PATH, keep_default_na=False)
    entities = pd.read_csv(cfg.ENTITIES_PATH, keep_default_na=False)
    counterparties = pd.read_csv(cfg.COUNTERPARTIES_PATH, keep_default_na=False)
    return transactions, entities, counterparties


def build_warehouse(conn: sqlite3.Connection | None = None,
                    rebuild: bool = True) -> tuple[sqlite3.Connection, dict[str, float]]:
    """Create the schema and load the generated CSVs. Returns timings."""
    if rebuild and cfg.DB_PATH.exists():
        cfg.DB_PATH.unlink()
    conn = conn or connect()

    timings = {"schema_seconds": run_sql_file(conn, SCHEMA_SQL)}

    transactions, entities, counterparties = load_frames()
    started = time.perf_counter()
    entities[ENTITY_COLUMNS].to_sql("entities", conn, if_exists="append", index=False)
    counterparties[COUNTERPARTY_COLUMNS].to_sql(
        "counterparties", conn, if_exists="append", index=False
    )
    transactions[TRANSACTION_COLUMNS].to_sql(
        "transactions", conn, if_exists="append", index=False, chunksize=10_000
    )
    conn.commit()
    timings["load_seconds"] = time.perf_counter() - started
    timings["rows_loaded"] = float(len(transactions))
    return conn, timings


def build_features(conn: sqlite3.Connection) -> tuple[pd.DataFrame, float]:
    """Run sql/02_features.sql and read the resulting table back."""
    elapsed = run_sql_file(conn, FEATURES_SQL)
    features = pd.read_sql_query("SELECT * FROM features", conn)
    return features, elapsed


def build_all(verbose: bool = True) -> tuple[pd.DataFrame, dict[str, float]]:
    conn, timings = build_warehouse()
    try:
        features, feature_seconds = build_features(conn)
    finally:
        conn.close()
    timings["features_seconds"] = feature_seconds

    if verbose:
        print(
            f"  schema      {timings['schema_seconds']:6.2f}s\n"
            f"  load {int(timings['rows_loaded']):,} rows "
            f"{timings['load_seconds']:6.2f}s\n"
            f"  features    {feature_seconds:6.2f}s  -> {len(features):,} rows x "
            f"{len(features.columns)} cols"
        )
    return features, timings


if __name__ == "__main__":
    build_all()
