# Findings

> Synthetic data throughout. Every number here is read from
> [`results/metrics.json`](../results/metrics.json), produced by
> `python run_pipeline.py`. The cost constants are invented — see
> [`config.py`](../config.py).

Seven things this project measured that I did not know before running it.

---

## 1. The blend ranks far better than either part, and barely changes the decision

| | PR-AUC | Optimal threshold | Precision | Recall | Alerts | Expected cost |
|---|---:|---:|---:|---:|---:|---:|
| Rules only | 0.518 | 50.0 | 0.640 | 0.950 | 89 | $81,800 |
| Model only | 0.198 | 98.9 | 0.152 | 0.983 | 388 | $94,912 |
| **Blended** | **0.774** | 39.6 | 0.164 | **1.000** | 366 | **$65,025** |

Blending lifts PR-AUC by 49% over rules alone. At the cost-optimal operating
point it saves **$1,487**, about 1.8%, and finds three more cases.

**So what:** the model's value is in ordering the queue, not in deciding what
enters it. If an analyst team works top-down through a fixed daily budget, the
ranking is what they experience and the lift is real. If the alert list is
worked exhaustively, the model is close to decoration. Which of those a team
does should decide how much the model is worth to them — and that is not a
question the PR-AUC answers.

---

## 2. A covariance matrix gets within 15% of IsolationForest, 34× faster

| Model | PR-AUC | ROC-AUC | Fit + score |
|---|---:|---:|---:|
| IsolationForest | 0.198 | 0.951 | 13.3s |
| Mahalanobis distance | 0.169 | 0.936 | **0.4s** |
| Local Outlier Factor | 0.084 | 0.879 | 21.4s |

Mahalanobis has no hyperparameters, no trees and no neighbour search — a
centroid and an inverse covariance.

**So what:** IsolationForest ships because it is ahead, but on a fold count this
small that gap is not obviously bigger than noise, and the operational
difference is enormous. On a stream where scoring latency matters, the
statistics-101 answer is the better engineering trade and the sophisticated
answer would need to justify itself.

Local Outlier Factor loses outright and takes the longest doing it.

---

## 3. Structuring evades the rule only when the depositor spreads *and* thins

R1 requires three cash deposits inside a seven-day window. The planted slow
variant spreads deposits across 9–14 days. Naively I expected all four slow
cases to escape. One did not.

| Case | Variant | Deposits | Span | Densest 7-day window | R1 fires |
|---|---|---:|---:|---:|---|
| CASE-0046 | slow | 6 | 10.7d | **4** | yes |
| CASE-0023 | slow | 3 | 9.7d | 2 | no |
| CASE-0031 | slow | 3 | 10.8d | 2 | no |
| CASE-0032 | slow | 3 | 10.1d | 2 | no |

Six deposits spread over ten days still leaves four inside some week.

**So what:** "widen the window" is the obvious fix and the wrong one — it would
also multiply false positives from cash-intensive businesses, which already
supply 492 legitimate deposits inside R1's band. The condition that actually
distinguishes these cases is *deposit density*, not span, which suggests a
density-based rule rather than a wider fixed window.

---

## 4. The model found every case the rules missed, and the score threw them away

All three missed cases were ranked by the IsolationForest in the **top 1%** of
100,299 transactions (percentiles 0.990, 0.992, 0.992).

The risk score is `100 × (0.6 × rules + 0.4 × model)`. A transaction that trips
no rule is capped at **40 points** regardless of model confidence. At the
conservative threshold of 67.9, they cannot get in.

**So what:** this is a policy, not a bug — "we do not alert on model suspicion
alone" is a defensible position when every alert has to be explained. But it has
a price, and the price is measurable: three cases, or a fourfold increase in
alert volume to recover them. A team should choose that knowingly. An obvious
next step is a model-only escalation lane with its own much higher bar, which
would keep the explainability policy for the main queue while not discarding a
top-1% signal outright.

---

## 5. Layering's false positives are one business model, not random noise

R2 fired 287 times: 68 true positives, 219 false positives. **216 of the 219 are
wholesale businesses** — 24 wholesalers out of 450 businesses, which receive a
large settlement and then run a supplier payment batch inside 48 hours.

**So what:** this is the most actionable finding in the set. A rule that
disqualifies known-supplier counterparties, or that requires the outbound legs
to be *new* counterparties, would remove most of that volume without touching
recall. That is a data-driven rule refinement rather than a threshold tweak, and
it is only visible because the false positives were attributed rather than
counted.

---

## 6. The transaction-level number is the wrong unit, and dormancy shows it

| Typology | Transaction recall | Case recall |
|---|---:|---:|
| Layering | 1.000 | 12/12 |
| Smurfing | 0.994 | 12/12 |
| Structuring | 0.895 | 12/12 |
| Round-dollar | 0.800 | 12/12 |
| **Dormant reactivation** | **0.640** | 12/12 |

R5 fires on the *first* transaction after a silence. Where a case reactivates
across two or three consecutive days, the later transactions have a one-day gap
and never trip it.

**So what:** a 0.640 transaction recall reads like a broken detector, and the
detector is fine — every dormant case reaches an analyst. Costing false
positives per transaction rather than per investigation inflates the expected
cost here by roughly 4×. The unit of measurement changed the conclusion, which
is why both levels are reported.

---

## 7. The cost-optimal threshold is mostly a statement about an invented constant

| If a missed case costs | Optimal threshold | Precision | Recall | Alerts |
|---|---:|---:|---:|---:|
| $5,000 | 67.9 | 0.731 | 0.950 | 78 |
| $25,000 *(default)* | 39.6 | 0.164 | 1.000 | 366 |
| $100,000 | 39.6 | 0.164 | 1.000 | 366 |

A 5× change in one number this project made up moves precision from 0.73 to 0.16
and alert volume from 78 to 366. Above $25,000 the recommendation is stable
across a 4× range.

Two related measurement notes:

- The cost surface **steps** wherever a case crosses the threshold. An integer
  0–100 sweep reported an optimum **19% more expensive** than the real one,
  because three cases sit between 39.6 and 39.7. The sweep now runs at 0.1
  resolution.
- The pooled optimum is chosen on the same data it is measured against. Choosing
  it on training folds instead, folds independently pick 39.6, 39.6, 39.9, 39.6,
  39.9 and held-out recall is **0.967** against a pooled 1.000. That gap is the
  optimism, and it is small.

**So what:** publish the sensitivity table next to the threshold, always. A
single recommended threshold quoted without it is an assumption wearing the
clothes of a finding.
