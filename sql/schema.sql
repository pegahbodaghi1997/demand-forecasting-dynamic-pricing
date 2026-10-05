PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingestion_run (
    run_id INTEGER PRIMARY KEY,
    source_sha256 TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    source_workbook BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    loaded_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('staged', 'loaded', 'failed')),
    error_message TEXT,
    raw_rows INTEGER NOT NULL DEFAULT 0,
    accepted_rows INTEGER NOT NULL DEFAULT 0,
    rejected_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0
);

-- Every parsed Excel data row is retained, including duplicates and rejected rows.
-- The BLOB in ingestion_run preserves the original workbook byte-for-byte.
CREATE TABLE IF NOT EXISTS raw_weekly (
    raw_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ingestion_run(run_id),
    sheet_name TEXT NOT NULL,
    excel_row INTEGER NOT NULL,
    week TEXT,
    product TEXT,
    sales_units TEXT,
    cost_of_goods_sold_cogs TEXT,
    inventory_level TEXT,
    demand_forecast TEXT,
    price_per_unit TEXT,
    gross_profit TEXT,
    operating_profit TEXT,
    revenue TEXT,
    inventory_turnover_ratio TEXT,
    gross_profit_margin TEXT,
    revenue_growth TEXT,
    extra_cells_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, sheet_name, excel_row)
);

CREATE INDEX IF NOT EXISTS idx_raw_weekly_run ON raw_weekly(run_id);

CREATE TABLE IF NOT EXISTS data_quality_issue (
    issue_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ingestion_run(run_id),
    raw_id INTEGER REFERENCES raw_weekly(raw_id),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    issue_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_quality_run ON data_quality_issue(run_id, issue_code);

CREATE TABLE IF NOT EXISTS dim_product (
    product_id INTEGER PRIMARY KEY,
    product_key TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL
);

-- Relative week numbers only: the source workbook has no calendar dates.
CREATE TABLE IF NOT EXISTS dim_week (
    week_id INTEGER PRIMARY KEY CHECK (week_id > 0),
    relative_year INTEGER NOT NULL CHECK (relative_year > 0),
    week_in_year INTEGER NOT NULL CHECK (week_in_year BETWEEN 1 AND 52)
);

CREATE TABLE IF NOT EXISTS fact_weekly_product (
    product_id INTEGER NOT NULL REFERENCES dim_product(product_id),
    week_id INTEGER NOT NULL REFERENCES dim_week(week_id),
    sales_units INTEGER NOT NULL CHECK (sales_units >= 0),
    unit_cost_cents INTEGER NOT NULL CHECK (unit_cost_cents >= 0),
    opening_inventory_units INTEGER CHECK (opening_inventory_units >= 0),
    historical_forecast_units INTEGER CHECK (historical_forecast_units >= 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    revenue_cents INTEGER NOT NULL CHECK (revenue_cents >= 0),
    gross_profit_cents INTEGER NOT NULL,
    operating_profit_cents INTEGER,
    turnover_ratio_source REAL,
    sales_recovered INTEGER NOT NULL DEFAULT 0 CHECK (sales_recovered IN (0, 1)),
    source_run_id INTEGER NOT NULL REFERENCES ingestion_run(run_id),
    source_raw_id INTEGER NOT NULL REFERENCES raw_weekly(raw_id),
    loaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (product_id, week_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_week ON fact_weekly_product(week_id);

CREATE VIEW IF NOT EXISTS vw_weekly_kpi AS
WITH financials AS (
    SELECT
        f.*,
        LAG(f.revenue_cents) OVER (
            PARTITION BY f.product_id ORDER BY f.week_id
        ) AS previous_revenue_cents
    FROM fact_weekly_product AS f
)
SELECT
    p.product_name,
    f.week_id,
    w.relative_year,
    w.week_in_year,
    f.sales_units,
    f.opening_inventory_units,
    f.historical_forecast_units,
    f.unit_cost_cents / 100.0 AS unit_cost,
    f.unit_price_cents / 100.0 AS unit_price,
    f.revenue_cents / 100.0 AS revenue,
    f.gross_profit_cents / 100.0 AS gross_profit,
    f.operating_profit_cents / 100.0 AS operating_profit,
    CASE WHEN f.revenue_cents > 0
        THEN 1.0 * f.gross_profit_cents / f.revenue_cents
    END AS gross_profit_margin,
    CASE WHEN f.previous_revenue_cents > 0
        THEN 1.0 * (f.revenue_cents - f.previous_revenue_cents)
             / f.previous_revenue_cents
    END AS revenue_growth,
    f.turnover_ratio_source,
    f.sales_recovered,
    f.source_run_id
FROM financials AS f
JOIN dim_product AS p ON p.product_id = f.product_id
JOIN dim_week AS w ON w.week_id = f.week_id;
