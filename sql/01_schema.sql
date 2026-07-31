-- ---------------------------------------------------------------------------
-- Schema for the transaction-monitoring warehouse.
--
-- Runs as-is on SQLite so the repo needs no database server, but written so a
-- Postgres deployment is a mechanical diff rather than a rewrite (C6). Every
-- place the two dialects part company is marked PORTABILITY below, and the
-- README carries the consolidated list.
--
-- Amounts are stored as INTEGER cents, never as floats. Round-dollar detection
-- is a modulo test and the SQL/Python parity contract is exact equality, and
-- both of those die quietly on binary floating point.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS features;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS entities;
DROP TABLE IF EXISTS counterparties;

-- ---------------------------------------------------------------------------
-- Monitored entities
-- ---------------------------------------------------------------------------
CREATE TABLE entities (
    entity_id           TEXT    NOT NULL PRIMARY KEY,   -- PORTABILITY: VARCHAR(16)
    entity_type         TEXT    NOT NULL,
    display_name        TEXT    NOT NULL,
    segment             TEXT    NOT NULL,
    home_region         TEXT    NOT NULL,
    opened_date         TEXT    NOT NULL,               -- PORTABILITY: DATE
    payroll_base_cad    REAL    NOT NULL,               -- PORTABILITY: NUMERIC(12,2)
    rent_cad            REAL    NOT NULL,
    typical_amount_cad  REAL    NOT NULL,
    CHECK (entity_type IN ('individual', 'business'))
);

-- ---------------------------------------------------------------------------
-- External counterparties
--
-- Deliberately a separate pool rather than other monitored entities: this
-- models a single institution's view, where the far side of a payment usually
-- sits somewhere else. It also means one payment is exactly one row, so a
-- ground-truth label is never ambiguous about which side it belongs to.
-- ---------------------------------------------------------------------------
CREATE TABLE counterparties (
    counterparty_id     TEXT    NOT NULL PRIMARY KEY,
    counterparty_type   TEXT    NOT NULL,
    display_name        TEXT    NOT NULL,
    home_region         TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Transactions -- one row per monitored payment, at entity grain
--
-- is_suspicious / typology / typology_variant / case_id are GROUND TRUTH.
-- They exist for evaluation only and are never selected into the feature
-- layer below. tests/test_no_label_leakage.py enforces that.
-- ---------------------------------------------------------------------------
CREATE TABLE transactions (
    transaction_id      TEXT    NOT NULL PRIMARY KEY,
    entity_id           TEXT    NOT NULL REFERENCES entities (entity_id),
    counterparty_id     TEXT,                           -- empty for cash deposits
    txn_datetime        TEXT    NOT NULL,               -- PORTABILITY: TIMESTAMP
    txn_epoch           INTEGER NOT NULL,               -- PORTABILITY: BIGINT
    amount_cents        INTEGER NOT NULL,               -- PORTABILITY: BIGINT
    direction           TEXT    NOT NULL,
    channel             TEXT    NOT NULL,
    is_cash             INTEGER NOT NULL,               -- PORTABILITY: BOOLEAN
    is_suspicious       INTEGER NOT NULL,               -- PORTABILITY: BOOLEAN
    typology            TEXT,
    typology_variant    TEXT,
    case_id             TEXT,
    CHECK (amount_cents > 0),
    CHECK (direction IN ('inbound', 'outbound')),
    CHECK (is_cash IN (0, 1)),
    CHECK (is_suspicious IN (0, 1))
);

-- Every window in 02_features.sql partitions by entity and orders by
-- txn_epoch; the counterparty index serves the as-of network features.
CREATE INDEX idx_txn_entity_epoch ON transactions (entity_id, txn_epoch);
CREATE INDEX idx_txn_counterparty_epoch ON transactions (counterparty_id, txn_epoch);
CREATE INDEX idx_txn_epoch ON transactions (txn_epoch);

-- ---------------------------------------------------------------------------
-- PORTABILITY SUMMARY -- what changes for Postgres
--
--   1. TEXT                  -> VARCHAR(n) where a bound is known.
--   2. INTEGER 0/1 flags     -> BOOLEAN, and `= 1` comparisons become the bare
--                               column. SQLite has no boolean type.
--   3. INTEGER amounts       -> BIGINT (or NUMERIC(18,2) if you would rather
--                               store dollars; the modulo test then needs a
--                               cast).
--   4. txn_datetime          -> TIMESTAMP, and the txn_epoch helper column
--                               becomes unnecessary: Postgres supports
--                               `RANGE BETWEEN INTERVAL '7 days' PRECEDING`
--                               directly on a timestamp ORDER BY, which SQLite
--                               does not. txn_epoch exists purely so the same
--                               window frames run on both.
--   5. strftime('%H', ts)    -> EXTRACT(HOUR FROM ts)
--      strftime('%w', ts)    -> EXTRACT(DOW FROM ts)   (same 0=Sunday coding)
--   6. CREATE TABLE .. AS    -> identical, though a materialised view is the
--                               better shape for a real deployment.
--   7. The correlated subqueries in 02_features.sql would be rewritten as
--      lateral joins; see the note there.
-- ---------------------------------------------------------------------------
