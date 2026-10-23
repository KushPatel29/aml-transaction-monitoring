#!/usr/bin/env bash
# Kill one monitor pod, without warning, while the producer is mid-stream.
#
# --grace-period=0 --force sends SIGKILL: no clean consumer close, no final
# offset commit. The group notices only when the session times out, and the
# surviving pod takes over partitions whose windows it has never seen. The
# verifier then has to find the stream identical to the batch anyway.
set -euo pipefail
ns="${NAMESPACE:-aml-stream}"
after="${KILL_AFTER_MESSAGES:-30000}"

for _ in $(seq 1 300); do
  if kubectl -n "$ns" logs job/producer 2>/dev/null | grep -q "\"messages\": ${after},"; then
    victim=$(kubectl -n "$ns" get pods -l app=monitor -o jsonpath='{.items[0].metadata.name}')
    kubectl -n "$ns" delete pod "$victim" --grace-period=0 --force
    echo "killed ${victim} at $(date -u +%H:%M:%S) after ${after} messages were produced"
    exit 0
  fi
  sleep 1
done
echo "the producer never reached ${after} messages; nothing was killed"
exit 1
