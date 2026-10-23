"""
Real-time transaction monitoring: the batch rules, run on a stream.

The batch pipeline (src/rules_engine.py) sees a year of transactions at once.
A bank's monitoring sees them one at a time, out of order, sometimes twice,
from consumers that crash. This package runs two windowed rules under those
conditions and is held to one standard: on the same transactions, the stream
must flag exactly what the batch flags.

    R1_STRUCTURING  three or more cash deposits between $7,500 and $9,999.99
                    within seven days (the batch rule, same thresholds)
    LCTR_24H        cash received totalling $10,000 or more within 24
                    consecutive hours: FINTRAC's 24-hour rule for large cash
                    transaction reports, which the batch pipeline lacks

windows.py and worker.py are the logic and need nothing but the standard
library. kafka_io.py runs them against Kafka (Redpanda); terraform/ deploys
the lot to Kubernetes.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
