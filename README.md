# Weekly commodity data pipeline

This project implements the data engineering stage of the technical assessment.
It archives each input Excel workbook in SQLite, retains every parsed data row in
a raw table, validates and cleans the rows, and loads a small star schema. The
provided workbook is already loaded in the local generated database at
data/assessment.sqlite. Forecasting and the dashboard are the next stages.

## Data flow

~~~text
Excel workbook (.xlsx / .xlsm)
    |-- exact workbook bytes --> ingestion_run.source_workbook
    |-- every parsed row -----> raw_weekly
                                |-- validation and quality issues
                                |-- equivalent duplicates removed
                                |-- conflicting product/week rows rejected
                                v
                    dim_product + dim_week + fact_weekly_product
                                               |
                                               v
                                        vw_weekly_kpi
~~~

The workbook archive and raw rows are committed before the clean load. If
validation or loading fails, the original workbook remains in
ingestion_run.source_workbook; the run is marked failed. A successful retry
can use the staged raw rows. A file with the same SHA-256 hash is skipped after
a successful load, so rerunning it does not multiply rows.

## Local setup (Windows PowerShell)

From this directory:

~~~powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m assessment_etl load .\Technical_Assessment\Sample_Data.xlsx
.\.venv\Scripts\python.exe -m assessment_etl status
~~~

SQLite is included with Python. The optional sqlite3 command-line program
can also inspect the generated file:

~~~powershell
sqlite3 .\data\assessment.sqlite ".tables"
sqlite3 .\data\assessment.sqlite "SELECT COUNT(*) FROM fact_weekly_product;"
~~~

An alternative database path can be passed before the command:

~~~powershell
.\.venv\Scripts\python.exe -m assessment_etl --db .\data\other.sqlite load .\path\other.xlsx
~~~

The database file and virtual environment are generated artifacts and are
excluded by .gitignore. The supplied assessment files are also kept out of
Git until their publication status is decided. The SQL schema and pipeline
source are versioned. The sample-specific integration check is skipped if
the supplied workbook is not present.

## Excel input contract

The loader does not hardcode the sample's 30 products or 260 weeks. It accepts
any .xlsx or .xlsm workbook with one or more worksheets containing a
recognizable header within the first 10 rows. Header case, punctuation,
spacing, column order, and common aliases are tolerated. Additional columns
are stored in raw_weekly.extra_cells_json. Every recognized data row,
including malformed and duplicate rows, is kept in the raw table. Unrecognized
worksheets are still preserved in the archived workbook BLOB.

Week and Product identify a record. A clean record also needs a
non-negative unit cost, a positive unit price, and non-negative integer sales.
When sales are absent or invalid, they can be recovered only if revenue divided
by price implies a whole number of units within one cent. Inventory and the
historical forecast are nullable. Monetary source values are compared with
recalculated values; mismatches are logged and the calculated values are used.
Rows with invalid required fields are rejected from the fact table while
remaining in the raw table.

If multiple rows in one workbook have the same product/week and identical
cleaned measures, one fact row is loaded and the extra raw rows are logged as
equivalent duplicates. If their measures conflict, all candidates for that key
are excluded from the current load and logged. A later workbook can update a
previous fact row with a new value; both workbooks and their raw rows remain
archived.

## Tables and data types

| Table/view | Purpose |
| --- | --- |
| ingestion_run | File hash, exact workbook BLOB, status, and row counts. |
| raw_weekly | Source sheet and row number, original cell values as text, extra columns as JSON. |
| data_quality_issue | Severity, code, and reason for each flagged raw row. |
| dim_product | Product key and display name. |
| dim_week | Relative week number and derived 52-week cycle position. |
| fact_weekly_product | One cleaned row per product/week, with source row provenance. |
| vw_weekly_kpi | Human-scale monetary values, margin, and revenue growth. |

Counts and week IDs use SQLite INTEGER. Unit cost, unit price, revenue,
gross profit, and operating profit are stored as integer cents to avoid
floating-point money errors. Source turnover is stored as REAL. The Excel
file does not specify a currency, so none is assumed.

Revenue and Gross_Profit are recalculated from sales, price, and unit
cost. Operating_Profit and Inventory_Turnover_Ratio are kept from the
source because the workbook lacks the inputs needed to reconstruct them.
Margin and revenue growth are computed in the view; they are NULL when
their denominators are zero or when there is no previous week.

dim_week.relative_year and week_in_year divide sequential weeks into
52-week blocks. These are relative cycle labels, not calendar dates.

## Cleaning and anomaly policy

- Blank historical Demand_Forecast values stay NULL. The pipeline does
  not fill them with realized sales.
- Zero sales and zero opening inventory remain valid observations.
- Invalid or non-finite source Revenue_Growth, including inf, is logged;
  the view computes a mathematically defined growth value where possible.
- Sales exceeding opening inventory, zero source turnover with positive sales,
  price at or below cost, and week-to-week jumps are flagged but not
  automatically deleted. In-week receipts and business events are not
  available in the workbook.
- The source inventory turnover definition is unknown. Do not derive a
  formal DSI from this column without its calculation period and average
  inventory definition.
- No customer, receivables, payment, or default records are supplied, so
  DSO and customer default ratings cannot be calculated from this dataset.

The sample workbook produced 7,839 archived raw rows, 7,800 clean fact rows,
39 equivalent duplicates, 16 recovered sales values, and no rejected rows.
Warnings and informational issues are counted per raw row, including
duplicates.

## Verification

Run the integration checks:

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

The checks cover exact workbook archive bytes, raw and fact row counts,
idempotent reloads, reordered and aliased headers, additional columns,
recovered sales, malformed rows, conflicting duplicates, later-file updates,
and archival of an unreadable workbook.

Useful SQLite queries:

~~~sql
SELECT status, raw_rows, accepted_rows, rejected_rows, duplicate_rows
FROM ingestion_run;

SELECT COUNT(*) FROM raw_weekly;
SELECT COUNT(*) FROM dim_product;
SELECT COUNT(*) FROM dim_week;
SELECT COUNT(*) FROM fact_weekly_product;

SELECT issue_code, COUNT(*) AS count
FROM data_quality_issue
GROUP BY issue_code
ORDER BY count DESC;

SELECT product_name, week_id, sales_units, revenue
FROM vw_weekly_kpi
ORDER BY product_name, week_id
LIMIT 10;
~~~

## Next stage

Build a time-based forecasting baseline and model using the cleaned weekly
fact data. Keep the 26 future forecast weeks in a separate forecast fact
table so predictions are not mixed with actual sales.
