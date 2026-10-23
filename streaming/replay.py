"""
The order the stream delivers transactions in: event order, roughened the way
real feeds are. A seeded few percent arrive up to six hours late (a branch
batch, a retried API call) and a seeded fraction arrive twice (a producer
retry after a lost acknowledgement). Six hours is half the worker's allowed
lateness, so nothing here should ever be too late: the test that checks the
stream against the batch would expose a window that slipped.
"""

from __future__ import annotations

import random

from .windows import Event

SEED = 2026
DELAY_SHARE = 0.03
MAX_DELAY_SECONDS = 6 * 3_600
DUPLICATE_SHARE = 0.005
MAX_DUPLICATE_GAP_SECONDS = 3_600


def events(frame) -> list[Event]:
    return [
        Event(
            r.transaction_id,
            r.entity_id,
            int(r.txn_epoch),
            int(r.amount_cents),
            r.direction,
            int(r.is_cash),
        )
        for r in frame.itertuples(index=False)
    ]


def schedule(evts: list[Event], seed: int = SEED) -> list[Event]:
    rng = random.Random(seed)
    sends: list[tuple[int, int, Event]] = []
    for seq, e in enumerate(sorted(evts, key=lambda e: (e.txn_epoch, e.transaction_id))):
        delay = rng.randint(1, MAX_DELAY_SECONDS) if rng.random() < DELAY_SHARE else 0
        sends.append((e.txn_epoch + delay, seq, e))
        if rng.random() < DUPLICATE_SHARE:
            sends.append((e.txn_epoch + delay + rng.randint(0, MAX_DUPLICATE_GAP_SECONDS), seq, e))
    sends.sort(key=lambda s: (s[0], s[1]))
    return [e for _, _, e in sends]


def partition_of(entity_id: str, partitions: int) -> int:
    """Stand-in for the producer's key partitioner in local simulations: what
    matters is that one entity always lands on one partition."""
    import zlib

    return zlib.crc32(entity_id.encode()) % partitions
