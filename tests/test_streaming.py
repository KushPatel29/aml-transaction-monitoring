"""
The streaming monitor, held to the batch rules without a broker.

These run the same PartitionWorker the Kubernetes deployment runs, fed in the
order the producer replays (late arrivals, duplicates) and split across
partitions by entity. The cluster test in .github/workflows/streaming.yml
repeats the comparison on real Kafka with a monitor pod killed mid-stream.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streaming import batch, replay
from streaming.windows import DAY, Event, LargeCash24h, Structuring
from streaming.worker import ALLOWED_LATENESS, PartitionWorker

ROOT = Path(__file__).resolve().parents[1]


def cash(txn: str, epoch: int, dollars: float, entity: str = "ENT-1") -> Event:
    return Event(txn, entity, epoch, int(round(dollars * 100)), "inbound", 1)


def run(events, worker=None):
    worker = worker or PartitionWorker()
    alerts = {}
    for offset, e in enumerate(events):
        for a in worker.process(e, offset):
            alerts[a.key] = a
    return worker, alerts


def flagged(alerts, rule):
    return {m for a in alerts.values() if a.rule == rule for m in a.members}


@pytest.fixture(scope="module")
def frame():
    return batch.load()


@pytest.fixture(scope="module")
def reference(frame):
    return batch.reference(frame)


@pytest.fixture(scope="module")
def deliveries(frame):
    return replay.schedule(replay.events(frame))


def stream(deliveries, partitions=6, crash_at=None):
    """Run the deliveries through one worker per partition. With crash_at, the
    worker for partition 0 loses its state after that many of its messages and
    a fresh one replays the partition from offset 0, quiet until it passes the
    offset the first had reached (its last commit)."""
    per_partition: dict[int, list[Event]] = {}
    for e in deliveries:
        per_partition.setdefault(replay.partition_of(e.entity_id, partitions), []).append(e)
    alerts, workers = {}, []
    for p, evts in sorted(per_partition.items()):
        if crash_at is not None and p == 0:
            first, early = run(evts[:crash_at])
            alerts.update(early)
            worker = PartitionWorker(live_from=crash_at)
        else:
            worker = PartitionWorker()
        w, late = run(evts, worker)
        alerts.update(late)
        workers.append(w)
    return alerts, workers


def test_the_stream_flags_exactly_what_the_batch_flags(deliveries, reference):
    alerts, workers = stream(deliveries)
    for rule, expected in reference.items():
        assert flagged(alerts, rule) == expected, rule
    assert sum(w.stats.too_late for w in workers) == 0
    assert sum(w.stats.duplicates for w in workers) > 0, "the replay should exercise duplicates"


def test_a_crashed_consumer_rebuilds_and_still_matches(deliveries, reference):
    alerts, workers = stream(deliveries, crash_at=5_000)
    for rule, expected in reference.items():
        assert flagged(alerts, rule) == expected, rule
    assert workers[0].stats.replayed == 5_000


def test_the_replacement_is_quiet_until_it_passes_the_commit(deliveries):
    evts = [e for e in deliveries if replay.partition_of(e.entity_id, 6) == 0][:5_000]
    worker, alerts = run(evts, PartitionWorker(live_from=len(evts)))
    assert alerts == {} and worker.stats.alerts == 0 and worker.retained() > 0


def test_the_replay_is_late_and_repeated_but_never_too_late(frame, deliveries):
    assert len(deliveries) > len(frame)
    assert replay.MAX_DELAY_SECONDS < ALLOWED_LATENESS
    order = {
        e.transaction_id: i
        for i, e in enumerate(sorted(deliveries, key=lambda e: (e.txn_epoch, e.transaction_id)))
    }
    assert any(
        order[a.transaction_id] > order[b.transaction_id]
        for a, b in zip(deliveries, deliveries[1:], strict=False)
    )


def test_state_stays_bounded(deliveries):
    _, workers = stream(deliveries)
    assert sum(w.retained() for w in workers) < 1_000
    assert sum(w.stats.evicted for w in workers) > 1_000


def test_r1_uses_the_batch_rules_thresholds():
    r = Structuring()
    assert r.window_seconds == 7 * DAY and r.min_deposits == 3
    assert r.candidate(cash("a", 0, 7_500)) and r.candidate(cash("b", 0, 9_999.99))
    assert not r.candidate(cash("c", 0, 7_499.99)) and not r.candidate(cash("d", 0, 10_000))


def test_structuring_window_edges_are_inclusive():
    t = 1_760_000_000
    _, alerts = run([cash("a", t, 8_000), cash("b", t + DAY, 8_000), cash("c", t + 7 * DAY, 8_000)])
    assert flagged(alerts, "R1_STRUCTURING") == {"a", "b", "c"}
    _, alerts = run(
        [cash("a", t, 8_000), cash("b", t + DAY, 8_000), cash("c", t + 7 * DAY + 1, 8_000)]
    )
    assert flagged(alerts, "R1_STRUCTURING") == set()


def test_a_late_deposit_completes_a_window_it_belongs_to():
    t = 1_760_000_000
    hour = 3_600
    # b happened between a and c but arrives after c, five hours late
    _, alerts = run(
        [cash("a", t, 8_000), cash("c", t + 10 * hour, 8_000), cash("b", t + 5 * hour, 8_000)]
    )
    assert flagged(alerts, "R1_STRUCTURING") == {"a", "b", "c"}


def test_the_24_hour_rule_sums_cash_across_consecutive_hours():
    t = 1_760_000_000
    _, alerts = run([cash("a", t, 4_000), cash("b", t + DAY - 60, 6_000)])
    assert flagged(alerts, "LCTR_24H") == {"a", "b"}
    _, alerts = run([cash("a", t, 4_000), cash("b", t + DAY + 1, 6_000)])
    assert flagged(alerts, "LCTR_24H") == set()
    _, alerts = run([cash("a", t, 10_000)])
    assert flagged(alerts, "LCTR_24H") == {"a"}
    _, alerts = run([cash("a", t, 9_999.99)])
    assert flagged(alerts, "LCTR_24H") == set()


def test_a_redelivered_deposit_is_not_counted_twice():
    t = 1_760_000_000
    worker, alerts = run([cash("a", t, 6_000), cash("a", t, 6_000)])
    assert flagged(alerts, "LCTR_24H") == set() and worker.stats.duplicates == 1


def test_cash_out_and_wires_are_not_cash_received():
    t = 1_760_000_000
    out = Event("o", "ENT-1", t, 1_200_000, "outbound", 1)
    wire = Event("w", "ENT-1", t, 1_200_000, "inbound", 0)
    _, alerts = run([out, wire])
    assert alerts == {}
    assert not LargeCash24h().candidate(out) and not LargeCash24h().candidate(wire)


def test_entities_never_share_a_window():
    t = 1_760_000_000
    _, alerts = run([cash("a", t, 6_000, "ENT-1"), cash("b", t + 60, 6_000, "ENT-2")])
    assert alerts == {}


def test_an_event_older_than_the_watermark_is_set_aside_not_folded_in():
    t = 1_760_000_000
    worker, alerts = run(
        [cash("a", t, 6_000), cash("z", t + ALLOWED_LATENESS + 3_600, 1), cash("b", t + 60, 6_000)]
    )
    assert worker.stats.too_late == 1 and flagged(alerts, "LCTR_24H") == set()


def test_a_growing_window_keeps_its_key():
    t = 1_760_000_000
    hour = 3_600
    _, alerts = run(
        [
            cash("a", t, 8_000),
            cash("c", t + 10 * hour, 8_000),
            cash("b", t + 5 * hour, 8_000),
            cash("d", t + 5 * hour + 60, 8_000),
        ]
    )
    # The window a..c qualified when b arrived; d joined it later and the
    # same key was re-published with four members. a..d is a window of its own.
    r1 = {k for k, a in alerts.items() if a.rule == "R1_STRUCTURING"}
    assert r1 == {"R1_STRUCTURING|ENT-1|a|c", "R1_STRUCTURING|ENT-1|a|d"}
    assert alerts["R1_STRUCTURING|ENT-1|a|c"].members == ("a", "b", "d", "c")


def test_the_lctr_sql_and_the_python_rule_agree_on_their_definition(frame, reference):
    """The SQL reference is not the only judge: recompute the 24-hour rule
    in plain Python, deposit by deposit, and require the same answer."""
    cash_rows = frame[(frame.is_cash == 1) & (frame.direction == "inbound")]
    hits = set()
    for _, g in cash_rows.groupby("entity_id"):
        t, a, ids = g.txn_epoch.tolist(), g.amount_cents.tolist(), g.transaction_id.tolist()
        for j in range(len(t)):
            members = [i for i in range(len(t)) if t[j] - DAY <= t[i] <= t[j]]
            if sum(a[i] for i in members) >= 1_000_000:
                hits.update(ids[i] for i in members)
    assert hits == reference["LCTR_24H"]


def test_the_readme_states_the_streaming_numbers(reference):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    r1, lctr = len(reference["R1_STRUCTURING"]), len(reference["LCTR_24H"])
    assert f"{r1} structuring deposits" in text
    assert f"{lctr:,} cash transactions under the 24-hour rule" in text


def test_the_image_ships_only_what_the_stream_reads():
    dockerfile = (ROOT / "streaming" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in dockerfile and "COPY . ." not in dockerfile
    main_tf = (ROOT / "streaming" / "terraform" / "main.tf").read_text(encoding="utf-8")
    assert main_tf.count("wait_for_completion = true") == 3
    assert "read_only_root_filesystem" in main_tf
