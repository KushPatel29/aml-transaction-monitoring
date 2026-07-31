"""
Cost arithmetic and the naive baseline.

Every dollar figure this module produces is downstream of four invented
constants in config.py. They are not industry benchmarks and the README says so
in the same breath as it quotes them. What is meaningful is the *shape* -- where
the cost curve turns, how far the optimum moves when the assumptions move --
and the sensitivity sweep exists so that shape can be read without anyone
mistaking $212.50 for a real number.

One correction to the brief's cost model is implemented here deliberately.
Costing false positives per *transaction* overcharges: an entity with six
flagged transactions is one investigation, not six. Both are computed. The
entity-level figure is the one the README draws conclusions from, and the
difference between them is itself reported.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

import config as cfg  # noqa: E402

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)


@dataclass
class Confusion:
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def precision(self) -> float:
        flagged = self.true_positives + self.false_positives
        return self.true_positives / flagged if flagged else 0.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def alerts(self) -> int:
        return self.true_positives + self.false_positives

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "alerts": self.alerts,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


def confusion(y_true: np.ndarray, flagged: np.ndarray) -> Confusion:
    y_true = y_true.astype(bool)
    flagged = flagged.astype(bool)
    return Confusion(
        true_positives=int((flagged & y_true).sum()),
        false_positives=int((flagged & ~y_true).sum()),
        false_negatives=int((~flagged & y_true).sum()),
        true_negatives=int((~flagged & ~y_true).sum()),
    )


def expected_cost(matrix: Confusion,
                  cost_per_investigation: float = cfg.COST_PER_INVESTIGATION_CAD,
                  cost_per_miss: float = cfg.PROXY_COST_PER_MISSED_CASE_CAD) -> float:
    """
    Investigating a false positive burns analyst hours; missing a true positive
    costs the proxy figure. True positives are not free either -- they also take
    an investigation -- but that cost is unavoidable for any system worth
    running, so it is excluded to keep the comparison between thresholds clean.
    """
    return matrix.false_positives * cost_per_investigation + matrix.false_negatives * cost_per_miss


def consolidate_to_entities(entity_ids: np.ndarray, y_true: np.ndarray,
                            flagged: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Roll transaction-level flags up to the unit an analyst actually works.

    An entity is a true case if any of its transactions is suspicious, and is
    alerted if any of its transactions crosses the threshold. This is what turns
    "571 flagged transactions" into "how many files land on someone's desk".
    """
    frame = pd.DataFrame(
        {"entity_id": entity_ids, "y_true": y_true.astype(bool), "flagged": flagged.astype(bool)}
    )
    rolled = frame.groupby("entity_id", sort=True).max()
    return rolled["y_true"].to_numpy(), rolled["flagged"].to_numpy()


def naive_baseline_flags(amount_cad: np.ndarray,
                         threshold: float = cfg.NAIVE_BASELINE_THRESHOLD_CAD) -> np.ndarray:
    """C8: flag every transaction over $10,000 CAD. The thing to beat."""
    return amount_cad > threshold


def analyst_capacity_alerts_per_week() -> float:
    """How many investigations the illustrative team can actually absorb."""
    return cfg.ANALYST_HEADCOUNT * cfg.ANALYST_HOURS_PER_WEEK / cfg.INVESTIGATION_HOURS_PER_ALERT


def evaluate(y_true: np.ndarray, flagged: np.ndarray, entity_ids: np.ndarray,
             cost_per_miss: float = cfg.PROXY_COST_PER_MISSED_CASE_CAD) -> dict:
    """Both levels, side by side, for one set of flags."""
    txn = confusion(y_true, flagged)
    entity_true, entity_flagged = consolidate_to_entities(entity_ids, y_true, flagged)
    ent = confusion(entity_true, entity_flagged)

    weeks = 52.0
    return {
        "transaction_level": {
            **txn.as_dict(),
            "expected_cost_cad": expected_cost(txn, cost_per_miss=cost_per_miss),
        },
        "entity_level": {
            **ent.as_dict(),
            "expected_cost_cad": expected_cost(ent, cost_per_miss=cost_per_miss),
            "alerts_per_week": ent.alerts / weeks,
            "analyst_capacity_per_week": analyst_capacity_alerts_per_week(),
            "within_capacity": bool(
                ent.alerts / weeks <= analyst_capacity_alerts_per_week()
            ),
        },
    }
