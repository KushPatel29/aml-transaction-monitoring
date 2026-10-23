"""
The state one consumer keeps for one partition, and the rules for when it may
speak.

Event time, not arrival time, decides every window. The watermark trails the
latest event time seen by ALLOWED_LATENESS; an event older than the watermark
is too late to be placed honestly, so it is counted and set aside rather than
silently folded into windows whose alerts were already published.

A consumer that crashes loses this state. Its replacement replays the
partition from the start to rebuild it, but publishes nothing until it passes
the offset the crashed consumer had committed: everything before that was
already published (offsets are committed only after alerts are flushed).
Duplicates are dropped by transaction ID, so a redelivered $6,000 deposit
cannot turn into $12,000 of cash. Only deposits a rule keeps need this: a
duplicate of anything else changes no window.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .windows import DAY, Alert, EntityWindows, Event, Rule, default_rules

ALLOWED_LATENESS = 12 * 3_600
EVICT_EVERY = 2_000


@dataclass
class Stats:
    processed: int = 0
    replayed: int = 0
    duplicates: int = 0
    too_late: int = 0
    alerts: int = 0
    evicted: int = 0


@dataclass
class PartitionWorker:
    live_from: int = 0
    rules: list[Rule] = field(default_factory=default_rules)
    lateness: int = ALLOWED_LATENESS
    max_event_time: int | None = None
    stats: Stats = field(default_factory=Stats)

    def __post_init__(self) -> None:
        self.state: dict[str, dict[str, EntityWindows]] = {
            r.code: defaultdict(EntityWindows) for r in self.rules
        }
        self.seen: dict[str, set[str]] = defaultdict(set)

    @property
    def watermark(self) -> int | None:
        return None if self.max_event_time is None else self.max_event_time - self.lateness

    def process(self, e: Event, offset: int) -> list[Alert]:
        live = offset >= self.live_from
        self.stats.processed += 1
        self.stats.replayed += not live
        wm = self.watermark
        if wm is not None and e.txn_epoch < wm:
            self.stats.too_late += 1
            return []
        if e.transaction_id in self.seen[e.entity_id]:
            self.stats.duplicates += 1
            return []
        self.max_event_time = (
            e.txn_epoch if self.max_event_time is None else max(self.max_event_time, e.txn_epoch)
        )

        alerts: list[Alert] = []
        kept = False
        for rule in self.rules:
            if not rule.candidate(e):
                continue
            kept = True
            windows = self.state[rule.code][e.entity_id]
            windows.insert(e)
            alerts.extend(windows.windows_touching(rule, e.entity_id, e.txn_epoch))
        if kept:
            self.seen[e.entity_id].add(e.transaction_id)
        if self.stats.processed % EVICT_EVERY == 0:
            self.evict()
        if not live:
            return []
        self.stats.alerts += len(alerts)
        return alerts

    def evict(self) -> None:
        """Drop deposits no future window can reach: anything older than the
        watermark minus the rule's window. A future event is at or after the
        watermark, so the windows it touches start no earlier than that."""
        wm = self.watermark
        if wm is None:
            return
        for rule in self.rules:
            for windows in self.state[rule.code].values():
                self.stats.evicted += windows.evict_before(wm - rule.window_seconds)
        for entity_id in list(self.seen):
            keep = set()
            for rule in self.rules:
                w = self.state[rule.code].get(entity_id)
                if w:
                    keep.update(k[1] for k in w.keys)
            self.seen[entity_id] = keep
            if not keep:
                del self.seen[entity_id]

    def retained(self) -> int:
        return sum(len(w.times) for per in self.state.values() for w in per.values())


__all__ = ["ALLOWED_LATENESS", "DAY", "PartitionWorker", "Stats"]
