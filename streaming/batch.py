"""
What the stream is held to: the same rules, computed in batch.

R1 is the batch pipeline's own function, src/rules_engine.rule_structuring,
not a re-implementation. The 24-hour large cash rule has no batch version in
the pipeline, so it is written here as SQL over SQLite: a self-join that sums
each entity's cash in the 24 hours ending at every deposit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

import config as cfg
from src.rules_engine import SORT_KEYS, rule_structuring

COLUMNS = [
    "transaction_id",
    "entity_id",
    "counterparty_id",
    "txn_epoch",
    "amount_cents",
    "direction",
    "is_cash",
]

LCTR_SQL = """
with cash as (
    select transaction_id, entity_id, txn_epoch, amount_cents
    from transactions
    where is_cash = 1 and direction = 'inbound'
),
qualifying as (                 -- every 24-hour window, ending at a deposit, that reaches $10,000
    select a.transaction_id as window_end, a.entity_id, a.txn_epoch as end_epoch
    from cash a
    join cash b
      on b.entity_id = a.entity_id
     and b.txn_epoch between a.txn_epoch - 86400 and a.txn_epoch
    group by a.transaction_id, a.entity_id, a.txn_epoch
    having sum(b.amount_cents) >= 1000000
)
select distinct b.transaction_id, b.entity_id
from qualifying q
join cash b
  on b.entity_id = q.entity_id
 and b.txn_epoch between q.end_epoch - 86400 and q.end_epoch
"""


def load(path: Path | None = None) -> pd.DataFrame:
    frame = pd.read_csv(
        path or cfg.TRANSACTIONS_PATH,
        usecols=COLUMNS,
        dtype={"counterparty_id": str},
        keep_default_na=False,
    )
    return frame.sort_values(SORT_KEYS, kind="mergesort").reset_index(drop=True)


def structuring(frame: pd.DataFrame) -> set[str]:
    fired, _ = rule_structuring(frame)
    return set(frame.loc[fired, "transaction_id"])


def large_cash_24h(frame: pd.DataFrame) -> set[str]:
    with sqlite3.connect(":memory:") as con:
        # Building the index spills to a temporary file by default, and the
        # verifier's root filesystem is read-only. Keep it in memory.
        con.execute("pragma temp_store = memory")
        frame.to_sql("transactions", con, index=False)
        con.execute("create index ix_cash on transactions(entity_id, txn_epoch)")
        return {row[0] for row in con.execute(LCTR_SQL)}


def reference(frame: pd.DataFrame) -> dict[str, set[str]]:
    return {"R1_STRUCTURING": structuring(frame), "LCTR_24H": large_cash_24h(frame)}
