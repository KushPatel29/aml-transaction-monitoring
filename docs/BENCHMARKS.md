# Benchmarks

Measured on the committed dataset — **100,299 transactions, 1,500 entities,
6,000 counterparties** — on a Windows 11 laptop, Python 3.12, SQLite 3.43.1.
Figures come from `runtime_seconds` in
[`results/metrics.json`](../results/metrics.json), written by
`python run_pipeline.py`.

## End to end

| Stage | Time | Notes |
|---|---:|---|
| Generate synthetic data | 2.65s | 1,500 entities, five typology injectors |
| Build schema | 0.09s | `sql/01_schema.sql` |
| Load into SQLite | 1.09s | 100,299 rows, three tables, three indexes |
| **Feature layer (SQL)** | **2.75s** | `sql/02_features.sql`, 17 columns |
| Rules R1–R5 | 0.40s | all five, with reason codes |
| Model bake-off | 29.43s | 5 models × 5 entity-disjoint folds = 25 fits |
| Sweep, bootstrap, sensitivity | ~9s | 1,001 thresholds + 2,000 resamples |
| **Total** | **45.6s** | |

The model bake-off is 65% of the run and four fifths of that is two models that
lose. Dropping LOF and Mahalanobis would take the pipeline under 20 seconds, and
they stay in because reporting the losses is the point.

## SQL versus pandas on the same feature layer

| | Time | Notes |
|---|---:|---|
| `sql/02_features.sql` on SQLite | 2.75s | three correlated subqueries, four window frames |
| `src/features.py` in pandas | 0.42s | same 17 columns, same values |

The pandas mirror is 6.5× faster and it is not the one that ships. The SQL is
the definition — it is what a reviewer reads, what ports to Postgres, and what
would run inside a warehouse next to the data instead of pulling 100k rows into
a process. The mirror exists to prove the definition, and
[`test_sql_python_feature_parity`](../tests/test_sql_python_feature_parity.py)
checks all 17 columns across all 100,299 rows.

## Where the SQL time goes

Three features have no windowed form in SQLite *or* Postgres and are correlated
subqueries:

| Feature | Shape |
|---|---|
| `distinct_counterparties_7d` | `COUNT(DISTINCT …)` is not a window function |
| `counterparty_entity_degree_asof` | as-of degree, against a pre-aggregated pair table |
| `new_counterparty_ratio_7d` | trailing window joined to first-seen dates |

The second of those was the one worth optimising. Counting distinct entities
directly over `transactions` scans every row a hub counterparty ever touched,
for every one of its transactions — quadratic in the busiest counterparties.
Pre-aggregating to `(counterparty, entity) → first epoch` first turns that into
a scan of a table two orders of magnitude smaller. Same answer, and the whole
feature layer stays under three seconds.

Indexes that matter: `(entity_id, txn_epoch)` for every window and the 7-day
subquery, `(counterparty_id, txn_epoch)` for the network features.

## Scaling notes

At 100k rows nothing here needs defending. What would change further up:

- **Correlated subqueries** become lateral joins on Postgres, and at warehouse
  scale a self-join with `GROUP BY` — or approximate distinct counting, since
  `distinct_counterparties_7d` is a signal, not an accounting figure.
- **The RANGE window frames scale fine.** They are a single ordered pass per
  partition, which is what warehouse engines are built for. Partitioning by
  entity means the work shards cleanly.
- **The rules engine is the part that would need rewriting.** It runs a Python
  loop per entity — 0.4s at 1,500 entities, and the wrong shape at a million.
  R4 and R5 are already pure vectorised expressions over the feature table; R1,
  R2 and R3 need forward-looking windows and would become SQL window functions
  or a Spark UDF over entity partitions.
- **LOF is the model that will not follow you.** Its 21.4s at 80k training rows
  is a neighbour search, and it scales worse than the IsolationForest it already
  loses to.

## Reproducibility

gzip stamps the current time into its header, so identical content produced a
different file on every run and dirtied the tree. `mtime` is pinned in
`config.GZIP`, and two consecutive pipeline runs now produce byte-identical
artefacts — which is what lets CI regenerate and assert nothing changed:

```yaml
- name: Assert the generated data is reproducible
  run: git diff --exit-code --stat -- data/generated/
```
