"""
The rules, run against Kafka. Four commands, one per Kubernetes workload:

    python -m streaming topics    create the topics (a Job, before anything else)
    python -m streaming produce   replay the year of transactions (a Job)
    python -m streaming process   the monitor itself (a Deployment, two replicas)
    python -m streaming verify    hold the stream to the batch (a Job, last)

Delivery is at least once and alerts are idempotent. The monitor commits a
partition's offset only after the alerts it produced are flushed, so a crash
loses nothing; the replacement rebuilds the partition's windows by replaying
it and stays quiet until it passes the committed offset, so a crash repeats
nothing that matters either. Alerts are keyed by window and the alerts topic
is compacted, so a repeat overwrites rather than adds.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from . import batch, replay
from .windows import Event
from .worker import PartitionWorker

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
TRANSACTIONS = os.environ.get("TRANSACTIONS_TOPIC", "transactions")
ALERTS = os.environ.get("ALERTS_TOPIC", "alerts")
PARTITIONS = int(os.environ.get("TRANSACTION_PARTITIONS", "6"))
GROUP = os.environ.get("MONITOR_GROUP", "aml-stream-monitor")
RATE = float(os.environ.get("PRODUCE_RATE", "4000"))  # messages per second, 0 = unthrottled
HEALTH_FILE = Path(os.environ.get("HEALTH_FILE", "/tmp/healthy"))
COMMIT_EVERY_SECONDS = 2.0


def log(**fields) -> None:
    print(json.dumps({"t": round(time.time(), 1), **fields}), flush=True)


# --------------------------------------------------------------------- topics
def topics() -> int:
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    wanted = [
        NewTopic(
            TRANSACTIONS,
            num_partitions=PARTITIONS,
            replication_factor=1,
            config={"retention.ms": "-1"},
        ),
        NewTopic(
            ALERTS, num_partitions=3, replication_factor=1, config={"cleanup.policy": "compact"}
        ),
    ]
    deadline = time.time() + 180
    while True:
        try:
            existing = set(admin.list_topics(timeout=10).topics)
            break
        except KafkaException as exc:
            if time.time() > deadline:
                raise
            log(event="waiting_for_broker", error=str(exc))
            time.sleep(3)
    for topic, fut in admin.create_topics([t for t in wanted if t.topic not in existing]).items():
        fut.result()
        log(event="topic_created", topic=topic)
    log(event="topics_ready", topics=[t.topic for t in wanted])
    return 0


# -------------------------------------------------------------------- produce
def produce() -> int:
    frame = batch.load()
    deliveries = replay.schedule(replay.events(frame))
    producer = Producer(
        {"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True, "acks": "all", "linger.ms": 20}
    )
    failures: list[str] = []

    def on_delivery(err, msg):
        if err is not None:
            failures.append(str(err))

    start = time.time()
    for i, e in enumerate(deliveries, 1):
        while True:
            try:
                producer.produce(
                    TRANSACTIONS,
                    key=e.entity_id,
                    value=json.dumps(e.to_json()),
                    timestamp=e.txn_epoch * 1000,
                    on_delivery=on_delivery,
                )
                break
            except BufferError:
                producer.poll(0.1)
        producer.poll(0)
        if RATE and i % 200 == 0:
            ahead = i / RATE - (time.time() - start)
            if ahead > 0:
                time.sleep(ahead)
        if i % 10_000 == 0:
            log(event="produced", messages=i, of=len(deliveries))
    producer.flush(60)
    log(
        event="produce_done",
        messages=len(deliveries),
        transactions=len(frame),
        failures=len(failures),
        seconds=round(time.time() - start, 1),
    )
    return 1 if failures else 0


# -------------------------------------------------------------------- process
def process() -> int:
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": GROUP,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "session.timeout.ms": 10_000,
            "heartbeat.interval.ms": 2_000,
            "max.poll.interval.ms": 120_000,
            "topic.metadata.refresh.interval.ms": 5_000,
        }
    )
    producer = Producer(
        {"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True, "acks": "all", "linger.ms": 20}
    )
    workers: dict[int, PartitionWorker] = {}
    positions: dict[int, int] = {}
    failed: list[str] = []
    stopping = False

    def on_delivery(err, msg):
        if err is not None:
            failed.append(str(err))

    def commit(parts=None) -> None:
        producer.flush(30)
        if failed:
            raise RuntimeError(f"alert delivery failed: {failed[:3]}")
        offsets = [
            TopicPartition(TRANSACTIONS, p, positions[p])
            for p in (parts or list(workers))
            if p in positions and positions[p] >= workers[p].live_from
        ]
        if offsets:
            consumer.commit(offsets=offsets, asynchronous=False)

    def on_assign(c, partitions):
        committed = c.committed(partitions, timeout=15)
        for tp in committed:
            live_from = tp.offset if tp.offset >= 0 else 0
            workers[tp.partition] = PartitionWorker(live_from=live_from)
            positions.pop(tp.partition, None)
            tp.offset = OFFSET_BEGINNING  # rebuild the windows from the start
        c.assign(committed)
        log(
            event="assigned",
            partitions={tp.partition: workers[tp.partition].live_from for tp in committed},
        )

    def on_revoke(c, partitions):
        mine = [tp.partition for tp in partitions if tp.partition in workers]
        try:
            commit(mine)
        except Exception as exc:  # the next owner replays from the last good commit
            log(event="commit_on_revoke_failed", error=str(exc))
        for p in mine:
            workers.pop(p, None)
            positions.pop(p, None)
        log(event="revoked", partitions=mine)

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    consumer.subscribe([TRANSACTIONS], on_assign=on_assign, on_revoke=on_revoke)
    last_commit = last_log = time.time()
    while not stopping:
        HEALTH_FILE.touch()
        for msg in consumer.consume(num_messages=500, timeout=1.0):
            if msg.error():
                log(event="consume_error", error=str(msg.error()))
                continue
            p = msg.partition()
            worker = workers.get(p)
            if worker is None:
                continue
            e = Event.from_json(json.loads(msg.value()))
            for alert in worker.process(e, msg.offset()):
                producer.produce(
                    ALERTS,
                    key=alert.key,
                    value=json.dumps(alert.to_json()),
                    on_delivery=on_delivery,
                )
            positions[p] = msg.offset() + 1
            producer.poll(0)
        now = time.time()
        if now - last_commit >= COMMIT_EVERY_SECONDS:
            commit()
            last_commit = now
        if now - last_log >= 5:
            log(
                event="stats",
                partitions={
                    p: vars(w.stats)
                    | {
                        "live_from": w.live_from,
                        "position": positions.get(p),
                        "retained": w.retained(),
                    }
                    for p, w in sorted(workers.items())
                },
            )
            last_log = now
    commit()
    consumer.close()
    log(event="stopped")
    return 0


# --------------------------------------------------------------------- verify
def _end_offsets(consumer: Consumer, topic: str) -> dict[int, int]:
    meta = consumer.list_topics(topic, timeout=15).topics[topic]
    return {
        p: consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=15)[1]
        for p in meta.partitions
    }


def verify() -> int:
    group_view = Consumer(
        {"bootstrap.servers": BOOTSTRAP, "group.id": GROUP, "enable.auto.commit": False}
    )
    deadline = time.time() + float(os.environ.get("VERIFY_TIMEOUT_SECONDS", "600"))
    caught_up = 0
    while caught_up < 3:
        ends = _end_offsets(group_view, TRANSACTIONS)
        committed = {
            tp.partition: tp.offset
            for tp in group_view.committed(
                [TopicPartition(TRANSACTIONS, p) for p in ends], timeout=15
            )
        }
        lag = {p: ends[p] - max(committed.get(p, 0), 0) for p in ends}
        caught_up = caught_up + 1 if sum(lag.values()) == 0 else 0
        log(event="waiting_for_monitor", lag=lag)
        if time.time() > deadline:
            log(event="verify_timeout", lag=lag)
            return 2
        time.sleep(3)
    group_view.close()

    reader = Consumer(
        {"bootstrap.servers": BOOTSTRAP, "group.id": "aml-verifier", "enable.auto.commit": False}
    )
    ends = _end_offsets(reader, ALERTS)
    reader.assign([TopicPartition(ALERTS, p, OFFSET_BEGINNING) for p in ends])
    latest: dict[str, dict] = {}
    remaining = {p for p, end in ends.items() if end > 0}
    read, idle_since = 0, time.time()
    while remaining and time.time() - idle_since < 30:
        batch_ = reader.consume(num_messages=1000, timeout=5.0)
        if batch_:
            idle_since = time.time()
        for msg in batch_:
            if msg.error():
                continue
            latest[msg.key().decode()] = json.loads(msg.value())
            read += 1
            if msg.offset() + 1 >= ends[msg.partition()]:
                remaining.discard(msg.partition())
    reader.close()
    if remaining:
        log(event="alerts_unreadable", partitions=sorted(remaining))
        return 2

    stream: dict[str, set[str]] = {}
    for alert in latest.values():
        stream.setdefault(alert["rule"], set()).update(alert["members"])
    reference = batch.reference(batch.load())
    result = {"alert_messages_read": read, "alert_windows": len(latest), "rules": {}}
    ok = True
    for rule, expected in reference.items():
        got = stream.get(rule, set())
        missing, extra = sorted(expected - got), sorted(got - expected)
        ok &= not missing and not extra
        result["rules"][rule] = {
            "batch": len(expected),
            "stream": len(got),
            "missing": missing[:20],
            "extra": extra[:20],
        }
    result["stream_equals_batch"] = ok
    log(event="verified", **result)
    return 0 if ok else 1


COMMANDS = {"topics": topics, "produce": produce, "process": process, "verify": verify}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(f"usage: python -m streaming {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]]()
