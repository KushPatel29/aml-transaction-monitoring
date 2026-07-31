-- ---------------------------------------------------------------------------
-- The feature layer. Fifteen features, one row per transaction.
--
-- This file is executed verbatim -- src/db.py reads it and hands it to SQLite
-- untouched, so the SQL a reviewer reads is the SQL that ran. src/features.py
-- reproduces every column below in pandas, and
-- tests/test_sql_python_feature_parity.py asserts the two agree on all 100,299
-- rows (C2).
--
-- Every definition here is written out longhand rather than tucked into a
-- helper, because the parity contract is only meaningful if the definition is
-- unambiguous. Where the two engines could legitimately disagree -- window
-- peer handling, empty denominators, missing history -- the behaviour is
-- pinned explicitly.
--
-- Window constants are in seconds and mirror config.py:
--     604800 = 7 days, 2592000 = 30 days, 172800 = 48 hours.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS features;

CREATE TABLE features AS
WITH
-- First time each counterparty appears anywhere in the data. Because this is
-- a MIN over all rows, it is always <= the epoch of any row referencing it,
-- so using it in an as-of feature cannot leak the future into the past.
cp_first_seen AS (
    SELECT
        counterparty_id,
        MIN(txn_epoch) AS cp_first_epoch
    FROM transactions
    WHERE counterparty_id IS NOT NULL
      AND counterparty_id <> ''
    GROUP BY counterparty_id
),

-- First time each (counterparty, entity) pair transacted. Counting pairs with
-- pair_first_epoch <= t gives the counterparty's degree as of t, and does it
-- against a table two orders of magnitude smaller than transactions.
cp_entity_pairs AS (
    SELECT
        counterparty_id,
        entity_id,
        MIN(txn_epoch) AS pair_first_epoch
    FROM transactions
    WHERE counterparty_id IS NOT NULL
      AND counterparty_id <> ''
    GROUP BY counterparty_id, entity_id
),

windowed AS (
    SELECT
        t.transaction_id,
        t.entity_id,
        t.counterparty_id,
        t.txn_datetime,
        t.txn_epoch,
        t.amount_cents,
        t.direction,
        t.is_cash,

        -- Trailing volume. RANGE (not ROWS) means the frame is defined by the
        -- ORDER BY value, so rows sharing an epoch are all peers and all land
        -- in the frame together. features.py reproduces that with a
        -- right-inclusive searchsorted rather than a row offset.
        COUNT(*)                                        OVER w7  AS txn_count_7d,
        SUM(t.amount_cents)                             OVER w7  AS txn_sum_7d_cents,
        COUNT(*)                                        OVER w30 AS txn_count_30d,
        SUM(t.amount_cents)                             OVER w30 AS txn_sum_30d_cents,

        SUM(CASE WHEN t.is_cash = 1
                 THEN t.amount_cents ELSE 0 END)        OVER w7  AS cash_sum_7d_cents,
        SUM(CASE WHEN t.direction = 'outbound'
                 THEN t.amount_cents ELSE 0 END)        OVER w48 AS outbound_48h_cents,
        SUM(CASE WHEN t.direction = 'inbound'
                 THEN t.amount_cents ELSE 0 END)        OVER w48 AS inbound_48h_cents,

        -- Expanding stats over STRICTLY PRIOR rows, for the entity-relative
        -- z-score. Excluding the current row is the whole point: a transaction
        -- must not contribute to the distribution it is being judged against.
        COUNT(*)                                        OVER wprior AS prior_n,
        SUM(CAST(t.amount_cents AS REAL))               OVER wprior AS prior_sum,
        SUM(CAST(t.amount_cents AS REAL)
            * CAST(t.amount_cents AS REAL))             OVER wprior AS prior_sumsq,

        LAG(t.txn_epoch)                                OVER wseq   AS prev_txn_epoch

    FROM transactions t
    WINDOW
        w7 AS (
            PARTITION BY t.entity_id ORDER BY t.txn_epoch
            RANGE BETWEEN 604800 PRECEDING AND CURRENT ROW
        ),
        w30 AS (
            PARTITION BY t.entity_id ORDER BY t.txn_epoch
            RANGE BETWEEN 2592000 PRECEDING AND CURRENT ROW
        ),
        w48 AS (
            PARTITION BY t.entity_id ORDER BY t.txn_epoch
            RANGE BETWEEN 172800 PRECEDING AND CURRENT ROW
        ),
        -- transaction_id breaks epoch ties so the row ordering is total and
        -- deterministic. Without it LAG and the expanding frame would be
        -- free to disagree with pandas on tied timestamps.
        wseq AS (
            PARTITION BY t.entity_id ORDER BY t.txn_epoch, t.transaction_id
        ),
        wprior AS (
            PARTITION BY t.entity_id ORDER BY t.txn_epoch, t.transaction_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        )
),

stats AS (
    SELECT
        w.*,
        CASE WHEN w.prior_n > 0 THEN w.prior_sum / w.prior_n END AS prior_mean,
        -- One-pass variance: E[x^2] - E[x]^2. This is the numerically weaker
        -- identity, and it is used because it is what a single window pass can
        -- express. The parity test therefore compares SQL against BOTH a
        -- one-pass and a two-pass numpy implementation, so if cancellation
        -- ever bites at these magnitudes the test is where it surfaces.
        CASE WHEN w.prior_n > 0
             THEN (w.prior_sumsq / w.prior_n)
                  - ((w.prior_sum / w.prior_n) * (w.prior_sum / w.prior_n))
        END AS prior_var
    FROM windowed w
)

SELECT
    s.transaction_id,
    s.entity_id,
    s.counterparty_id,
    s.txn_epoch,
    s.amount_cents,
    s.direction,
    s.is_cash,

    -- F1-F4 ------------------------------------------------------------------
    s.txn_count_7d,
    s.txn_sum_7d_cents,
    s.txn_count_30d,
    s.txn_sum_30d_cents,

    -- F5 ---------------------------------------------------------------------
    -- Entity-relative z-score against the entity's own prior history.
    -- Falls back to 0.0 rather than NULL when there is too little history or
    -- no dispersion, so downstream consumers never have to special-case it.
    CASE
        WHEN s.prior_n < 5                       THEN 0.0
        WHEN s.prior_var IS NULL                 THEN 0.0
        WHEN s.prior_var <= 0                    THEN 0.0
        WHEN SQRT(s.prior_var) < 0.000000001     THEN 0.0
        ELSE (CAST(s.amount_cents AS REAL) - s.prior_mean) / SQRT(s.prior_var)
    END                                                     AS amount_zscore_entity,

    -- F6 ---------------------------------------------------------------------
    -- Exact multiple of $1,000. Integer modulo on cents, so this is exact on
    -- both sides -- the reason amounts are never stored as floats.
    CASE WHEN s.amount_cents % 100000 = 0 THEN 1 ELSE 0 END AS is_round_amount,

    -- F7 ---------------------------------------------------------------------
    -- -1.0 marks "this entity's first transaction", which is a different
    -- statement from "zero days since the last one".
    CASE
        WHEN s.prev_txn_epoch IS NULL THEN -1.0
        ELSE (s.txn_epoch - s.prev_txn_epoch) / 86400.0
    END                                                     AS days_since_prev_txn,

    -- F8 ---------------------------------------------------------------------
    -- COUNT(DISTINCT ...) is not a window function in SQLite OR Postgres, so
    -- this is a correlated subquery. Fine at 100k rows against the
    -- (entity_id, txn_epoch) index; at warehouse scale you would rewrite it as
    -- a self-join with GROUP BY, or reach for approximate distinct counting.
    (SELECT COUNT(DISTINCT t2.counterparty_id)
       FROM transactions t2
      WHERE t2.entity_id = s.entity_id
        AND t2.counterparty_id <> ''
        AND t2.txn_epoch >= s.txn_epoch - 604800
        AND t2.txn_epoch <= s.txn_epoch)                    AS distinct_counterparties_7d,

    -- F9 ---------------------------------------------------------------------
    -- Pass-through pressure. Capped, because an entity with a trivial inbound
    -- and a large outbound produces an unbounded ratio that would dominate any
    -- distance-based model. 0.0 when nothing came in at all.
    CASE
        WHEN s.inbound_48h_cents = 0 THEN 0.0
        ELSE MIN(CAST(s.outbound_48h_cents AS REAL)
                 / CAST(s.inbound_48h_cents AS REAL), 10.0)
    END                                                     AS outbound_to_inbound_ratio_48h,

    -- F10 --------------------------------------------------------------------
    -- Denominator is always positive: the current row is inside its own frame
    -- and every amount is > 0.
    CAST(s.cash_sum_7d_cents AS REAL)
        / CAST(s.txn_sum_7d_cents AS REAL)                  AS cash_ratio_7d,

    -- F11 --------------------------------------------------------------------
    -- How many distinct monitored entities this counterparty had dealt with as
    -- of this moment. Legitimate employers and utilities score high here by
    -- design, which is exactly why it is not a signal on its own.
    CASE
        WHEN s.counterparty_id = '' THEN 0
        ELSE (SELECT COUNT(*)
                FROM cp_entity_pairs p
               WHERE p.counterparty_id = s.counterparty_id
                 AND p.pair_first_epoch <= s.txn_epoch)
    END                                                     AS counterparty_entity_degree_asof,

    -- F12 --------------------------------------------------------------------
    CASE
        WHEN s.counterparty_id = '' THEN 0.0
        ELSE (s.txn_epoch
              - (SELECT f.cp_first_epoch
                   FROM cp_first_seen f
                  WHERE f.counterparty_id = s.counterparty_id)) / 86400.0
    END                                                     AS counterparty_age_days,

    -- F13 --------------------------------------------------------------------
    -- Share of the entity's trailing week spent with counterparties that did
    -- not exist a week ago. Cash deposits have no counterparty, so they are
    -- counted in the denominator but can never be "new" -- the inner join
    -- drops them from the numerator.
    CAST((SELECT COUNT(*)
            FROM transactions t4
            JOIN cp_first_seen f ON f.counterparty_id = t4.counterparty_id
           WHERE t4.entity_id = s.entity_id
             AND t4.txn_epoch >= s.txn_epoch - 604800
             AND t4.txn_epoch <= s.txn_epoch
             AND f.cp_first_epoch >= s.txn_epoch - 604800) AS REAL)
        / CAST(s.txn_count_7d AS REAL)                      AS new_counterparty_ratio_7d,

    -- F14-F15 ----------------------------------------------------------------
    -- PORTABILITY: EXTRACT(HOUR FROM ...) and EXTRACT(DOW FROM ...).
    -- SQLite's %w and Postgres' DOW share the 0=Sunday coding.
    CAST(strftime('%H', s.txn_datetime) AS INTEGER)         AS hour_of_day,
    CASE WHEN strftime('%w', s.txn_datetime) IN ('0', '6')
         THEN 1 ELSE 0 END                                  AS is_weekend,

    -- Rule support ------------------------------------------------------------
    -- R4 and R5 need the entity's prior history, which the z-score already
    -- computed above. Exposing it here rather than recomputing it privately in
    -- the rules engine means these two are covered by the same parity contract
    -- as everything else. They are NOT model features -- see
    -- config.RULE_SUPPORT_COLUMNS.
    s.prior_n                                               AS prior_txn_count,
    COALESCE(s.prior_mean, 0.0)                             AS prior_mean_cents

FROM stats s;

CREATE INDEX idx_features_entity ON features (entity_id, txn_epoch);
CREATE UNIQUE INDEX idx_features_txn ON features (transaction_id);
