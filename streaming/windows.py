"""
Event-time sliding windows, per entity, for rules of the form "these
transactions, inside any window of this length, add up to an alert".

A transaction is flagged if it lies in ANY qualifying window. The batch rule
anchors windows forward from each deposit; a stream can only look backward
from the newest one. The two flag the same transactions: a set of deposits
that fits in a window of length W fits in the window that starts at its first
member and in the one that ends at its last. So every window here ends at a
deposit, [t_end - W, t_end], both ends inclusive like the batch rule's.

Arrival order is not event order. When a deposit arrives late, every window it
could belong to is re-evaluated: those ending at a deposit no earlier than it
and no more than W after it.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import config as cfg

DAY = 86_400


@dataclass(frozen=True)
class Event:
    transaction_id: str
    entity_id: str
    txn_epoch: int
    amount_cents: int
    direction: str
    is_cash: int

    def to_json(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "entity_id": self.entity_id,
            "txn_epoch": self.txn_epoch,
            "amount_cents": self.amount_cents,
            "direction": self.direction,
            "is_cash": self.is_cash,
        }

    @classmethod
    def from_json(cls, d: dict) -> Event:
        return cls(
            str(d["transaction_id"]),
            str(d["entity_id"]),
            int(d["txn_epoch"]),
            int(d["amount_cents"]),
            str(d["direction"]),
            int(d["is_cash"]),
        )


@dataclass(frozen=True)
class Alert:
    rule: str
    entity_id: str
    window_start: int
    window_end: int
    total_cents: int
    members: tuple[str, ...]

    @property
    def key(self) -> str:
        """The window's identity. A late deposit that joins the window
        re-publishes it under the same key with more members, so a compacted
        topic keeps the fullest version and a replay cannot double-count."""
        return f"{self.rule}|{self.entity_id}|{self.members[0]}|{self.members[-1]}"

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "rule": self.rule,
            "entity_id": self.entity_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "total_cents": self.total_cents,
            "members": list(self.members),
        }


@dataclass
class Rule:
    code: str
    window_seconds: int

    def candidate(self, e: Event) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def qualifies(self, amounts: list[int]) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class Structuring(Rule):
    """R1, with the batch rule's own thresholds from config.py."""

    code: str = "R1_STRUCTURING"
    window_seconds: int = cfg.R1_WINDOW_DAYS * DAY
    floor_cents: int = int(round(cfg.R1_AMOUNT_FLOOR * 100))
    ceiling_cents: int = int(round(cfg.R1_AMOUNT_CEILING * 100))
    min_deposits: int = cfg.R1_MIN_DEPOSITS

    def candidate(self, e: Event) -> bool:
        return (
            e.is_cash == 1
            and e.direction == "inbound"
            and self.floor_cents <= e.amount_cents <= self.ceiling_cents
        )

    def qualifies(self, amounts: list[int]) -> bool:
        return len(amounts) >= self.min_deposits


@dataclass
class LargeCash24h(Rule):
    """FINTRAC's 24-hour rule: cash received from or on behalf of the same
    person totalling $10,000 or more within 24 consecutive hours is reported
    as one large cash transaction. A single deposit of $10,000 qualifies on
    its own. This is a reporting obligation, not a finding of suspicion."""

    code: str = "LCTR_24H"
    window_seconds: int = DAY
    threshold_cents: int = 1_000_000

    def candidate(self, e: Event) -> bool:
        return e.is_cash == 1 and e.direction == "inbound"

    def qualifies(self, amounts: list[int]) -> bool:
        return sum(amounts) >= self.threshold_cents


@dataclass
class EntityWindows:
    """One rule's deposits for one entity, kept in event-time order."""

    times: list[int] = field(default_factory=list)
    keys: list[tuple[int, str]] = field(default_factory=list)
    amounts: list[int] = field(default_factory=list)

    def insert(self, e: Event) -> None:
        k = (e.txn_epoch, e.transaction_id)
        i = bisect.bisect_left(self.keys, k)
        self.keys.insert(i, k)
        self.times.insert(i, e.txn_epoch)
        self.amounts.insert(i, e.amount_cents)

    def evict_before(self, epoch: int) -> int:
        i = bisect.bisect_left(self.times, epoch)
        if i:
            del self.times[:i], self.keys[:i], self.amounts[:i]
        return i

    def windows_touching(self, rule: Rule, entity_id: str, t: int) -> list[Alert]:
        """Every qualifying window that ends at a deposit in [t, t + W]."""
        w = rule.window_seconds
        alerts = []
        lo = bisect.bisect_left(self.times, t)
        hi = bisect.bisect_right(self.times, t + w)
        seen_end_times = set()
        for j in range(lo, hi):
            t_end = self.times[j]
            if t_end in seen_end_times:  # deposits sharing a timestamp share a window
                continue
            seen_end_times.add(t_end)
            a = bisect.bisect_left(self.times, t_end - w)
            b = bisect.bisect_right(self.times, t_end)
            amounts = self.amounts[a:b]
            if rule.qualifies(amounts):
                alerts.append(
                    Alert(
                        rule.code,
                        entity_id,
                        self.times[a],
                        t_end,
                        sum(amounts),
                        tuple(k[1] for k in self.keys[a:b]),
                    )
                )
        return alerts


def default_rules() -> list[Rule]:
    return [Structuring(), LargeCash24h()]
