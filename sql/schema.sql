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

-- Dashboard-ready weekly product KPIs. Opening stock cover is a historical
-- proxy based on one week's observed sales, not formal DSI or a stockout claim.
CREATE VIEW IF NOT EXISTS vw_kpi_product_week AS
SELECT
    f.product_id,
    p.product_name,
    f.week_id,
    w.relative_year,
    w.week_in_year,
    f.sales_units,
    f.opening_inventory_units,
    f.unit_cost_cents / 100.0 AS unit_cost,
    f.unit_price_cents / 100.0 AS unit_price,
    f.sales_units * f.unit_cost_cents / 100.0 AS cost_of_goods_sold,
    f.revenue_cents / 100.0 AS revenue,
    f.gross_profit_cents / 100.0 AS gross_profit,
    CASE WHEN f.revenue_cents > 0
        THEN 1.0 * f.gross_profit_cents / f.revenue_cents
    END AS gross_profit_margin,
    f.operating_profit_cents / 100.0 AS operating_profit,
    CASE WHEN f.revenue_cents > 0 AND f.operating_profit_cents IS NOT NULL
        THEN 1.0 * f.operating_profit_cents / f.revenue_cents
    END AS operating_profit_margin,
    CASE WHEN prior.revenue_cents > 0
        THEN 1.0 * (f.revenue_cents - prior.revenue_cents) / prior.revenue_cents
    END AS revenue_growth,
    CASE WHEN f.sales_units > 0 AND f.opening_inventory_units IS NOT NULL
        THEN 7.0 * f.opening_inventory_units / f.sales_units
    END AS opening_stock_cover_days_proxy,
    CASE WHEN f.opening_inventory_units IS NOT NULL
                   AND f.sales_units > f.opening_inventory_units
        THEN 1 ELSE 0
    END AS sales_exceed_opening_inventory,
    f.turnover_ratio_source,
    f.sales_recovered,
    f.source_run_id
FROM fact_weekly_product AS f
JOIN dim_product AS p ON p.product_id = f.product_id
JOIN dim_week AS w ON w.week_id = f.week_id
LEFT JOIN fact_weekly_product AS prior
  ON prior.product_id = f.product_id AND prior.week_id = f.week_id - 1;

-- Portfolio margins are ratios of summed cents, never averages of SKU margins.
-- Operating profit is NULL when even one product's value is unavailable.
CREATE VIEW IF NOT EXISTS vw_kpi_portfolio_week AS
WITH totals AS (
    SELECT
        week_id,
        COUNT(*) AS product_rows,
        SUM(sales_units) AS sales_units,
        SUM(sales_units * unit_cost_cents) AS cost_of_goods_sold_cents,
        SUM(revenue_cents) AS revenue_cents,
        SUM(gross_profit_cents) AS gross_profit_cents,
        SUM(operating_profit_cents) AS available_operating_profit_cents,
        SUM(CASE WHEN operating_profit_cents IS NOT NULL THEN 1 ELSE 0 END)
            AS operating_profit_rows,
        SUM(CASE WHEN opening_inventory_units IS NOT NULL THEN 1 ELSE 0 END)
            AS inventory_rows,
        SUM(CASE WHEN opening_inventory_units = 0 THEN 1 ELSE 0 END)
            AS zero_opening_inventory_rows,
        SUM(CASE WHEN opening_inventory_units IS NOT NULL
                      AND sales_units > opening_inventory_units THEN 1 ELSE 0 END)
            AS sales_exceed_opening_inventory_rows,
        SUM(sales_recovered) AS recovered_sales_rows
    FROM fact_weekly_product
    GROUP BY week_id
)
SELECT
    t.week_id,
    w.relative_year,
    w.week_in_year,
    t.product_rows,
    t.sales_units,
    t.cost_of_goods_sold_cents / 100.0 AS cost_of_goods_sold,
    t.revenue_cents / 100.0 AS revenue,
    t.gross_profit_cents / 100.0 AS gross_profit,
    CASE WHEN t.revenue_cents > 0
        THEN 1.0 * t.gross_profit_cents / t.revenue_cents
    END AS gross_profit_margin,
    CASE WHEN t.operating_profit_rows = t.product_rows
        THEN t.available_operating_profit_cents / 100.0
    END AS operating_profit,
    CASE WHEN t.operating_profit_rows = t.product_rows AND t.revenue_cents > 0
        THEN 1.0 * t.available_operating_profit_cents / t.revenue_cents
    END AS operating_profit_margin,
    CASE WHEN previous.revenue_cents > 0
        THEN 1.0 * (t.revenue_cents - previous.revenue_cents)
             / previous.revenue_cents
    END AS revenue_growth,
    t.operating_profit_rows,
    t.inventory_rows,
    t.zero_opening_inventory_rows,
    t.sales_exceed_opening_inventory_rows,
    t.recovered_sales_rows
FROM totals AS t
JOIN dim_week AS w ON w.week_id = t.week_id
LEFT JOIN totals AS previous ON previous.week_id = t.week_id - 1;

-- Forecasts are versioned and never inserted into the historical sales fact.
CREATE TABLE IF NOT EXISTS forecast_run (
    forecast_run_id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    input_max_week INTEGER NOT NULL,
    horizon_weeks INTEGER NOT NULL CHECK (horizon_weeks > 0),
    input_sha256 TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    selected_method TEXT NOT NULL,
    selection_rule TEXT NOT NULL,
    selected_parameters_json TEXT NOT NULL,
    development_metrics_json TEXT NOT NULL,
    holdout_metrics_json TEXT NOT NULL,
    training_rows INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_backtest (
    forecast_run_id INTEGER NOT NULL REFERENCES forecast_run(forecast_run_id),
    method TEXT NOT NULL,
    fold_number INTEGER NOT NULL,
    origin_week INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES dim_product(product_id),
    target_week INTEGER NOT NULL,
    actual_units INTEGER NOT NULL,
    predicted_units REAL NOT NULL CHECK (predicted_units >= 0),
    PRIMARY KEY (forecast_run_id, method, fold_number, product_id, target_week)
);

CREATE INDEX IF NOT EXISTS idx_forecast_backtest_method
ON forecast_backtest(forecast_run_id, method, fold_number);

CREATE TABLE IF NOT EXISTS fact_demand_forecast (
    forecast_run_id INTEGER NOT NULL REFERENCES forecast_run(forecast_run_id),
    product_id INTEGER NOT NULL REFERENCES dim_product(product_id),
    week_id INTEGER NOT NULL REFERENCES dim_week(week_id),
    predicted_units INTEGER NOT NULL CHECK (predicted_units >= 0),
    lower_error_band_units INTEGER NOT NULL CHECK (lower_error_band_units >= 0),
    upper_error_band_units INTEGER NOT NULL CHECK (upper_error_band_units >= 0),
    PRIMARY KEY (forecast_run_id, product_id, week_id),
    CHECK (lower_error_band_units <= predicted_units),
    CHECK (upper_error_band_units >= predicted_units)
);

CREATE VIEW IF NOT EXISTS vw_latest_demand_forecast AS
SELECT
    p.product_name,
    f.week_id,
    f.predicted_units,
    f.lower_error_band_units,
    f.upper_error_band_units,
    r.selected_method,
    r.forecast_run_id
FROM fact_demand_forecast AS f
JOIN forecast_run AS r ON r.forecast_run_id = f.forecast_run_id
JOIN dim_product AS p ON p.product_id = f.product_id
WHERE f.forecast_run_id = (SELECT MAX(forecast_run_id) FROM forecast_run);
