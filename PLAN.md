# PLAN — Transaction Monitoring & Anomaly Detection (Phase 0)

> **This is a synthetic, educational simulation.** All data is generated with Faker.
> It is not a compliance product, is not built on or representative of any real
> institution's data, and models only *publicly described* suspicious-transaction
> indicator categories, genericized. Nothing here references a real institution,
> a real case, or a real regulatory filing.

Repo: `aml-transaction-monitoring` (proposed name — say the word if you want
`transaction-monitoring-analytics` instead; it's a one-line change before Phase 1).
Local path: `C:\Users\kush2\Claude\portfolio-projects\aml-transaction-monitoring`.
Branch: `main`.

---

## 1. Repo structure

```
aml-transaction-monitoring/
├── README.md                     # five-step narrative, numbers from results/metrics.json
├── PLAN.md                       # this file
├── PORTFOLIO_CARD.md             # 2–3 line blurb for kushpatel29.github.io
├── LICENSE                       # MIT (matches your other repos)
├── requirements.txt              # pinned, free-tier only
├── config.py                     # ALL tunable constants in one place (C9)
├── run_pipeline.py               # single command: generate → load → feature → score → metrics
├── .gitignore
├── .streamlit/config.toml        # theme only, no secrets (C5)
├── .github/workflows/ci.yml      # pytest on every push (C4)
│
├── data/
│   ├── generate_transactions.py  # Faker generator + 5 typology injectors
│   └── generated/                # committed outputs (deterministic, seed in config.py)
│       ├── entities.csv
│       ├── counterparties.csv
│       └── transactions.csv
│
├── sql/
│   ├── 01_schema.sql             # DDL, written to port cleanly to Postgres (C6)
│   └── 02_features.sql           # the feature layer — windowed SQL, runs verbatim
│
├── src/
│   ├── __init__.py
│   ├── db.py                     # build SQLite db, execute sql/ files verbatim
│   ├── features.py               # Python mirror of 02_features.sql (C2)
│   ├── rules_engine.py           # R1–R5, each → (fired: bool, reason: str)
│   ├── anomaly_model.py          # IsolationForest + supervised comparison model
│   ├── risk_scorer.py            # 0–100 blend, threshold sweep
│   └── cost_model.py             # cost curve, optimal threshold, naive baseline
│
├── app/
│   └── streamlit_app.py          # triage dashboard (C5)
│
├── tests/
│   ├── conftest.py               # session fixture: build pipeline artefacts once
│   ├── fixtures/hand_built_cases.py
│   ├── test_generator_ground_truth.py
│   ├── test_sql_python_feature_parity.py
│   ├── test_rules_engine_known_cases.py
│   ├── test_metric_floors.py
│   ├── test_no_pii_in_data.py
│   └── test_dashboard_masks_identifiers.py
│
├── results/                      # committed — the README quotes these files (C3)
│   ├── metrics.json
│   ├── scored_transactions.csv
│   └── threshold_sweep.csv
│
└── docs/
    ├── FINDINGS.md               # per-typology recall, failure analysis
    └── *.png                     # PR curve, cost curve, screenshots
```

**Why `data/generated/` and `results/` are committed:** Streamlit Community Cloud
clones the repo and runs `streamlit run app/streamlit_app.py` — it never runs
`run_pipeline.py`. Committing the deterministic artefacts means the deployed app,
the README numbers, and CI all describe the *same* run. `run_pipeline.py`
regenerates them byte-for-byte from the seed; a test asserts that determinism.
Total committed size ≈ 4 MB.

---

## 2. Data model — the grain

**Grain: one row per transaction, from the monitored entity's point of view.**

| Table | Rows | Purpose |
|---|---|---|
| `entities` | 800–1,500 | monitored accounts (individuals + small businesses) |
| `counterparties` | ~2,000 | external parties the entities transact with |
| `transactions` | 10,000–20,000 | 12 simulated months |

`transactions` columns:

| Column | Type | Notes |
|---|---|---|
| `transaction_id` | TEXT | `TXN-000001` |
| `entity_id` | TEXT | FK → entities |
| `counterparty_id` | TEXT NULL | NULL for cash deposits |
| `txn_datetime` | TEXT (ISO) | |
| `txn_epoch` | INTEGER | seconds — the window `ORDER BY` key (see §7) |
| `amount_cents` | INTEGER | **integer cents, never floats** |
| `direction` | TEXT | `inbound` / `outbound` |
| `channel` | TEXT | `cash_deposit`, `wire`, `eft`, `etransfer`, `cheque`, `card` |
| `is_cash` | INTEGER | 0/1 |
| `is_suspicious` | INTEGER | **ground truth — evaluation only** |
| `typology` | TEXT NULL | ground truth enum |
| `case_id` | TEXT NULL | groups the transactions of one planted case |

Three design decisions worth your veto:

1. **Single-institution view.** Counterparties are an *external* pool, so every
   transfer produces exactly one row. A real system sometimes sees both legs
   (extra signal). This keeps the ground-truth label unambiguous — no arguing
   about whether a mule's leg counts. It goes in "what this misses" as a stated
   simplification, not a discovered one.
2. **Integer cents.** Round-dollar detection and SQL↔Python parity both die on
   float `%`. Cents make `is_round_amount` and the parity test exact.
3. **Ground truth never becomes a feature.** `is_suspicious`, `typology`, `case_id`
   are dropped before the feature matrix is built; a test asserts the
   IsolationForest's feature list contains none of them.

---

## 3. Baseline (legitimate) activity

Amounts are log-normal; timing is entity-type-specific so the noise is
*structured*, not uniform — that's what makes false positives realistic.

**Individuals** (≈70% of entities)
- Semi-monthly payroll inbound on the 15th and last business day, fixed base
  ±2% jitter, 07:00–10:00.
- 8–25 outbound/month, log-normal(μ = ln 90, σ = 0.9), peaks at 12:00–13:00
  and 17:00–21:00, ~25% lower volume on weekends.
- Cash deposits rare (~2% of their transactions).

**Businesses** (≈30% of entities)
- Inbound settlements weekdays 09:00–17:00, log-normal(μ = ln 2500, σ = 1.1).
- Supplier-payment **batches**: 3–8 outbound within one hour, Tue/Thu.
- Payroll outbound on the 15th and EOM.
- A `cash_intensive` sub-segment (~15% of businesses) makes regular cash deposits
  in the $2k–$12k range. **These are deliberately in R1's blast radius** — they
  are the honest source of structuring false positives, and the reason the
  precision number will not be flattering.

---

## 4. The five typologies — generation logic

Injected into **3% of entities by default** (`TYPOLOGY_ENTITY_RATE` in `config.py`,
tunable 2–4% per your spec), split evenly across the five, each behind its own
toggle in `TYPOLOGY_TOGGLES`. Each injector is a pure function
`(txns, entity, rng, cfg) -> (txns, case_record)` so it can add *and remove* rows
(T5 needs removal). Expect ~36 cases / ~1.5% of transactions labelled suspicious —
low, and deliberately so: the precision problem is the actual problem in this domain.

| # | Typology | Generation logic |
|---|---|---|
| **T1** | Structuring | `n ~ U{3..6}` cash deposits, each `U($8,000.00, $9,999.99)`, day offsets drawn without replacement from 0–6 (so ≤ 7-day window), banking hours, `channel=cash_deposit`, `is_cash=1`, `direction=inbound`. |
| **T2** | Rapid layering | Lump-sum inbound `L ~ U($25k, $150k)` by wire. Then `k ~ U{3..6}` outbound to **k distinct** counterparties within 4–36h, splitting `0.90–0.98 × L` (random split, no leg < 5% of L) → entity retains < 10%. All legs labelled. |
| **T3** | Smurfing | `m ~ U{10..18}` **distinct** counterparties send inbound within a 72h window; each amount `~ N(μ, 0.12μ)` with `μ ~ U($400, $1,800)`; arrival times spread across the window. |
| **T4** | Round-dollar anomaly | `j ~ U{4..8}` transactions with amounts that are exact multiples of $1,000, drawn at 3×–12× that entity's own historical **median**, and required to exceed its historical **p90**. Spread over 30–60 days. Defined *relative to the entity*, so a business whose legit amounts are already chunky is a harder case — as it should be. |
| **T5** | Dormant reactivation | Delete all baseline rows in a 90–180 day window, then emit 1–3 transactions at 5×–15× the entity's historical mean. Only the reactivation rows are labelled. |

**One question for you (§12, Q1):** I'd like ~25% of T1 cases generated as a
**slow-structuring variant** — same deposits spread over 9–14 days, tagged
`typology_variant='slow'`. It sits deliberately outside R1's 7-day window. Two
reasons: it's the realest evasion in this typology, and it gives the "what this
misses" section a *measured* number (recall on `fast` vs `slow`) instead of prose.
The cost: I'd be planting the limitation I then report. My proposal for keeping it
honest — declare the stress subset openly in the README *and* separately run a
blind failure analysis over the actual false negatives at the operating threshold,
then write §"what this misses" from whichever finding is more interesting. Your call.

---

## 5. Evaluation split

Entity-disjoint 70/30 split (`ENTITY_SPLIT_SEED`). Everything that touches labels
or thresholds is fit on train entities only, then evaluated on held-out entities.
This matters more than usual here: features are *entity-relative* (z-score against
own history), so splitting by transaction would leak an entity's history across the
boundary. The floors in `test_metric_floors.py` read held-out numbers only.

---

## 6. SQL feature layer + Python parity (C2)

Ten features, each computed twice — `sql/02_features.sql` (executed verbatim) and
`src/features.py` — with `test_sql_python_feature_parity` asserting equality on the
full generated set, not just a sample.

| # | Feature | Exact definition (both sides implement *this*) |
|---|---|---|
| F1/F2 | `txn_count_7d`, `txn_sum_7d_cents` | window over entity, `ORDER BY txn_epoch RANGE BETWEEN 604800 PRECEDING AND CURRENT ROW` (inclusive of current row) |
| F3/F4 | `txn_count_30d`, `txn_sum_30d_cents` | same, `2592000 PRECEDING` |
| F5 | `amount_zscore_entity` | over **prior rows only** (`ROWS UNBOUNDED PRECEDING AND 1 PRECEDING`), population σ via `sqrt(avg(x²) − avg(x)²)`; `0.0` if prior count < 5 or σ < 1e-9 |
| F6 | `is_round_amount` | `amount_cents % 100000 = 0` (exact multiple of $1,000) |
| F7 | `days_since_prev_txn` | `(txn_epoch − LAG(txn_epoch)) / 86400.0`; `−1.0` for an entity's first row |
| F8 | `distinct_counterparties_7d` | correlated subquery — `COUNT(DISTINCT …)` is **not** a window function in SQLite *or* Postgres |
| F9 | `outbound_to_inbound_ratio_48h` | `Σ outbound / NULLIF(Σ inbound, 0)` over `172800 PRECEDING`; `0.0` when no inbound; capped at 10.0 |
| F10 | `cash_ratio_7d` | `Σ cash / Σ all` over the 7-day frame |

Two things I want the parity test to be sharp about:
- F5 uses the one-pass `E[x²] − E[x]²` identity because that's what SQL can express
  in a single window pass. It's the numerically weaker formula. The test therefore
  compares **three** implementations — SQL, one-pass Python, two-pass Python
  (`np.std`) — at `atol=1e-6`. If cancellation ever bites, that test is where it
  shows up, and the README says so.
- F8's correlated subquery is O(n · window). Fine at 15k rows; the README's Postgres
  note says what changes at scale.

**Postgres portability note (C6)** — what `01_schema.sql` would need:
`TEXT` → `VARCHAR`, `INTEGER` 0/1 → `BOOLEAN`, `amount_cents INTEGER` → `BIGINT`
(or `NUMERIC(18,2)`), `txn_epoch` ordering → native
`RANGE BETWEEN INTERVAL '7 days' PRECEDING`, and `strftime()` → `date_trunc()`.
The schema is written so those are the *only* diffs.

---

## 7. Rules engine (R1–R5)

Each rule returns `(fired, reason_code)` e.g.
`"R1_STRUCTURING: 4 cash deposits totalling $34,200.00 in 6 days, each under $10,000"`.

**Rule thresholds are deliberately looser than the generator's** — otherwise the
rules are a fingerprint of the generator and every metric is a lie.

| Rule | Fires when | vs. generator |
|---|---|---|
| R1 | ≥3 cash inbound in trailing 7d, each in **$7,500–$9,999.99** | generator uses $8,000–$9,999.99 → rule also catches legit cash-intensive businesses |
| R2 | inbound ≥ **$15,000**, then ≥3 outbound to ≥3 distinct counterparties within 48h totalling ≥ **70%** | generator: ≥$25k, 90–98% |
| R3 | ≥**8** distinct inbound counterparties in 72h, amount CV < **0.35**, mean < **$5,000** | generator: 10–18 senders, CV ≈ 0.12, mean ≤ $1,800 |
| R4 | multiple of $1,000 **and** z-score ≥ 2.0 **and** ≥10 prior transactions | generator: 3–12× median, > p90 |
| R5 | `days_since_prev_txn` ≥ 90 **and** amount ≥ **4×** entity trailing mean | generator: 90–180d gap, 5–15× |

Each tested against a hand-built fixture that fires and a hand-built clean case
that must not (`tests/fixtures/hand_built_cases.py`).

---

## 8. Models, risk score, cost model

- **IsolationForest** on F1–F10 (+ hour-of-day, is_weekend, entity_type one-hot).
  No labels. `contamination` in `config.py`.
- **Supervised comparison** — logistic regression on the same features with the
  ground-truth labels, entity-disjoint split. Framed in the README as *"what you'd
  gain if you had clean historical labels, which teams in this space usually
  don't"* — a ceiling, not a shipped model.
- **Risk score** = `100 × (0.6 × rule_component + 0.4 × iforest_component)`,
  weights in `config.py`. `rule_component = min(1, rules_fired / 2)`;
  `iforest_component` = percentile rank of the anomaly score (fit on train).
- **Cost model** — `config.py`, in one clearly-labelled `ILLUSTRATIVE` block:

  ```python
  # ILLUSTRATIVE ONLY — invented for this simulation.
  # Not industry figures, not regulatory figures, not benchmarks. (C9)
  ANALYST_HOURLY_RATE_CAD          = 85.00
  INVESTIGATION_HOURS_PER_ALERT    = 2.5     # → $212.50 per alert
  PROXY_COST_PER_MISSED_CASE_CAD   = 25_000.00
  ```

  Sweep threshold 0→100; at each: precision, recall, F1, alerts,
  `cost = FP × 212.50 + FN × 25,000`. Report the cost-minimising threshold.
  Because that optimum is *entirely* a function of the invented $25k/$212.50 ratio,
  `metrics.json` also carries a **sensitivity block** re-running the sweep at
  $5k / $25k / $100k per missed case, and the README shows how far the optimum
  moves. That's the honest way to quote a cost number you made up.

- **Naive baseline (C8)** — flag every transaction > $10,000 CAD. Same precision /
  recall / cost math, same table. Prediction: it scores ~0 recall on structuring
  (every deposit is under $10k *by construction*) while alerting on a pile of
  ordinary business wires. If the layered system *doesn't* beat it on cost, the
  README says that instead — the comparison is the point, not the win.

---

## 9. Tests

| Test | Asserts |
|---|---|
| `test_generator_ground_truth` | planted patterns are statistically distinct: T1 deposits cluster in $8k–$10k within 7 days; T2 retention < 10%; T3 ≥ 10 distinct senders / 72h; T4 amounts exceed entity p90 and are round; T5 gap ≥ 90d and ≥ 5× mean. Plus: **suspicious ≠ trivially separable** — a single-feature amount threshold must *not* achieve the pipeline's recall. |
| `test_sql_python_feature_parity` | all 10 features, SQL vs Python vs two-pass, full dataset (C2) |
| `test_rules_engine_known_cases` | each of R1–R5 fires on its fixture, stays silent on the clean one |
| `test_metric_floors` | precision and recall at the operating threshold on **held-out** entities stay above frozen floors |
| `test_no_pii_in_data` | no SIN-shaped `\d{3}[- ]?\d{3}[- ]?\d{3}`, no Luhn-valid 13–19 digit strings, no real institution names, across every committed CSV (C1) |
| `test_dashboard_masks_identifiers` | the app's masking helper is applied to every identifier column it renders; raw `entity_id` never reaches a rendered frame (C1) |
| `test_pipeline_determinism` | re-running the generator reproduces the committed CSVs' content hash |
| `test_no_label_leakage` | model feature list ∩ {`is_suspicious`,`typology`,`case_id`} = ∅ |

**On the floors:** I'll set them *after* the first real run, at ≈90% of observed,
with a comment saying they're regression guards — not targets I tuned toward. I'm
not going to pick a number now and then quietly tune the pipeline until it clears.

---

## 10. Dashboard (Phase 7)

Disclaimer banner pinned top (C1) → case queue (masked entity `ENT-••••1234`, risk
score, reason codes, typology guess, amount, sortable/filterable) → threshold slider
live-updating precision/recall/cost → PR curve + cost-vs-threshold charts →
per-entity drill-down (timeline, rules fired, feature values) → naive-baseline
toggle. Matplotlib/Altair only (both free-tier, no API keys).

---

## 11. Constraint traceability

| | Where it's satisfied |
|---|---|
| C1 | Faker-only generator; `test_no_pii_in_data`; banner in README header + `st.warning` in app |
| C2 | `sql/02_features.sql` ↔ `src/features.py`, `test_sql_python_feature_parity` |
| C3 | Every README number pulled from `results/metrics.json`; README states the command that produced it |
| C4 | `pip install -r requirements.txt && python run_pipeline.py`; `.github/workflows/ci.yml` |
| C5 | Streamlit + pandas + sklearn + altair only; no keys; `.streamlit/config.toml` |
| C6 | SQLite; schema + Postgres diff note (§6) |
| C7 | README five-step framing; "what this misses" from measured results |
| C8 | `src/cost_model.py` naive baseline, reported in README with numbers |
| C9 | `config.py` `ILLUSTRATIVE` block + sensitivity sweep |
| C10 | Genericized regions/channels; no institutions, cases, or filings named |

---

## 12. What I need from you before Phase 1

**Q1 — the slow-structuring variant.** Plant it (§4) or leave T1 clean and write
"what this misses" purely from post-hoc failure analysis? I lean *plant it, declare
it, and also do the blind analysis* — but you own the honesty standard here.

**Q2 — repo name.** `aml-transaction-monitoring` (proposed) or
`transaction-monitoring-analytics`? The first is the stronger keyword for the
FINTRAC/CBSA/CRA-adjacent roles; the second is more neutral.

**Q3 — scale.** I've defaulted to 1,200 entities / ~15,000 transactions / 3%
typology rate → ~36 cases. That's realistic but statistically thin: precision and
recall on ~11 held-out cases will be noisy, and the floors have to be loose to be
honest. I can raise it to ~25,000 transactions / 4% for tighter numbers at the cost
of a slightly bigger repo. Say the word or I'll go with the default.

**Two things I can't do for you:**
- `gh` CLI isn't installed on this machine, so **you'll need to create the empty
  GitHub repo**; I can push over your existing `~/.ssh/id_ed25519_portfolio` key
  once it exists.
- **Streamlit Community Cloud deployment needs your login.** I'll get the repo
  deploy-ready and verify the app boots headless locally, but the deploy click and
  the live URL for the README are yours. I'll leave the README link as a clearly
  marked placeholder until you paste it back.

---

## 13. Addendum — decisions confirmed, scope raised

Answers received: **plant the slow variant**, keep the name
**`aml-transaction-monitoring`**, and scale to **100,000 transactions**. What that
changes:

### 13.1 Scale and the per-entity arithmetic

100k transactions across the brief's 800–1,500 entities means ~67 transactions per
entity per year. Two consequences worth stating rather than burying:

- **N_ENTITIES = 1,500** (top of the brief's range), ~6,000 counterparties.
- The transaction set is **monitored payment activity** — deposits, transfers,
  wires, cheques, e-transfers, bill payments — **not retail card spend**. Real
  monitoring systems scope out POS card noise, and it's the only way 67
  transactions/entity/year reads as realistic rather than sparse. Individuals run
  ~4–5 monitored payments/month, businesses ~9–12.
- **`TYPOLOGY_ENTITY_RATE = 0.04`** (top of the brief's 2–4%) → **60 planted cases**,
  ~0.5% of transactions labelled suspicious. That base rate is punishing, and it is
  supposed to be — the precision problem *is* the problem in this domain.

### 13.2 How 60 cases get evaluated honestly

Sixty cases is too few for a single 70/30 split to say anything stable, so the
70/30 split in §5 is replaced by:

- **5-fold entity-disjoint cross-validation.** Every model, scaler and score
  normaliser is fit on train-fold entities and applied to held-out entities. Pooling
  the out-of-fold predictions yields one honest scored set covering **all 100k
  transactions and all 60 cases**, each held out exactly once.
- **Bootstrap 95% confidence intervals** (2,000 resamples, resampled *by entity*)
  on every headline metric. With 60 cases, a bare point estimate for PR-AUC or
  precision is close to meaningless — the README quotes intervals.
- **Nested threshold selection as an honesty check.** The cost-minimising threshold
  chosen on pooled out-of-fold data is mildly optimistic, because that data chose
  it. So the pipeline also selects the threshold on the *training* folds and applies
  it to the held-out fold, and reports the gap. Whatever that gap is, it goes in the
  README.

### 13.3 Alert consolidation — a correction to the brief's cost model

C8/Phase 5 specify `cost = FP × cost_per_investigation + FN × cost_per_miss` at the
transaction level. Taken literally that overcharges: one entity with six flagged
transactions is **one** investigation, not six. Both are computed and reported:

- **Transaction-level** metrics, exactly as specified — precision, recall, PR-AUC.
- **Entity-level (consolidated alert)** metrics, where an alert is an entity whose
  peak risk score crosses the threshold, and the cost model runs on *those* counts.

The README says which one drives the cost conclusion and why. Same treatment for
the naive baseline, so the comparison is like-for-like.

### 13.4 Model bake-off, not a single model

Following the house pattern of reporting losses: **IsolationForest vs
LocalOutlierFactor vs a plain statistical z-score baseline**, all unsupervised, all
under the same CV. Then the supervised logistic-regression ceiling on top. If
IsolationForest loses to the dumb z-score baseline, that's the finding and it gets
the headline.

### 13.5 Counterparty network layer

The counterparty pool is generated with a realistic degree distribution —
**employers, utilities and landlords are legitimate high-degree hubs**, so
"talks to many entities" is not by itself a signal. Against that noise:

- ~40% of layering cases draw their outbound legs from a **shared mule pool** of
  ~30 counterparties, which is how these networks actually behave.
- Three network features join the parity-tested set (F11–F13 below), all computed
  **as-of the transaction timestamp** — a full-period counterparty degree would leak
  the future into the past. A test asserts the as-of property directly.
- `src/network.py` builds the bipartite entity↔counterparty graph (networkx) and
  surfaces ring components in the dashboard drill-down.

### 13.6 Final feature list — 15, all parity-tested

F1–F10 as in §6, plus:

| # | Feature | Definition |
|---|---|---|
| F11 | `counterparty_entity_degree_asof` | distinct monitored entities this counterparty has transacted with **up to and including** this timestamp |
| F12 | `counterparty_age_days` | days since this counterparty's first appearance in the data, as-of |
| F13 | `new_counterparty_ratio_7d` | share of the entity's trailing-7d transactions whose counterparty was first seen within 7 days |
| F14 | `hour_of_day` | `strftime('%H', ...)` → `EXTRACT(HOUR FROM ...)` in Postgres |
| F15 | `is_weekend` | `strftime('%w', ...)` ∈ {0,6} |

### 13.7 Storage

100k rows of raw transactions plus a scored set with 15 features exceeds what
belongs in a git repo as plain CSV. Committed artefacts are **gzipped CSV**
(`.csv.gz`, read natively by pandas — no pyarrow dependency added to the
Streamlit Cloud image). Expected total ≈ 8–12 MB.
