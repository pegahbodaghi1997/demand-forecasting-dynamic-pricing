# Historical KPI definitions and data limits

## Scope

This layer reads only `fact_weekly_product`, the cleaned historical actuals.
It does not combine forecast rows with actual sales or assume a currency that
the workbook does not specify. Monetary amounts are stored as integer cents
and converted to display units only after aggregation. Weeks are sequential
IDs, not calendar dates.

`vw_kpi_product_week` provides one row per observed product and week.
`vw_kpi_portfolio_week` aggregates products by observed week. The `kpi`
command produces a compact JSON report with all-history, recent-period,
previous-period, latest-week, and product diagnostics. Views stay current
when cleaned facts are reloaded; no separate KPI table needs refreshing.
Margin and growth columns in the SQL views are fractions (for example,
`0.21` means 21%); JSON period-summary fields ending in `_pct` are percentages.

## KPI catalog

| KPI | Definition | Status and interpretation |
| --- | --- | --- |
| Revenue | Sum of `Sales_Units * Price_Per_Unit`. | Calculated from cleaned rows. |
| Cost of goods sold | Sum of `Sales_Units * unit acquisition cost`. | Calculated; source cost is per unit. |
| Gross profit | Revenue minus cost of goods sold. | Calculated, with source discrepancies logged during ingestion. |
| Gross profit margin | Total gross profit / total revenue. | Calculated when revenue is positive; portfolio margin is a ratio of sums, not an average of product margins. |
| Operating profit | Sum of source operating profit. | Source reported; a group total is NULL if any included row is missing. Expenses are absent, so it cannot be reconstructed. |
| Operating profit margin | Total operating profit / total revenue. | Calculated only if operating profit is complete and revenue is positive. |
| Revenue growth, weekly | Current revenue / immediately preceding week's revenue minus 1. | NULL for a missing prior week or zero prior revenue. Product and portfolio growth are computed separately. |
| Revenue growth, recent period | Recent period revenue / previous equal-length period revenue minus 1. | Reported only when both periods contain every product-week and previous revenue is positive. |
| Opening stock cover, days | `7 * opening inventory units / observed weekly sales units`. | Historical proxy; NULL when sales are zero or opening inventory is missing. It is not a formal DSI measure or proof of a stockout. |
| Source inventory turnover ratio | Original workbook value. | Available but unverified: its period and inventory basis are unspecified. Do not aggregate it as a standard turnover KPI. |
| Days sales of inventory (DSI) | Typically average inventory / period COGS times period days. | Unavailable as a formal KPI: ending or average inventory and in-week receipts are missing. |
| Days sales outstanding (DSO) | Typically average accounts receivable / credit sales times period days. | Unavailable: receivables, credit sales terms, and payment dates are missing. |
| Customer default risk rating | Requires customer exposure and repayment or default history. | Unavailable: the workbook has no customer-level credit records. |

The stock-cover proxy uses observed sales in the same week, not forecast
demand. A zero opening inventory with positive sales can reflect in-week
receipts; it does not establish a lost sale. At the latest observed week,
8 products have zero opening inventory and 9 sold more units than their
reported opening inventory. These are data and replenishment diagnostics,
not confirmed stockout alerts.

## Sample workbook result

| Measure | All 260 observed weeks | Recent weeks 248-260 |
| --- | ---: | ---: |
| Sales units | 9,741,185 | 489,871 |
| Revenue, source currency unspecified | 314,978,072.24 | 16,042,252.47 |
| Gross profit | 67,835,130.73 | 3,447,249.22 |
| Gross profit margin | 21.54% | 21.49% |
| Source operating profit | 36,906,208.77 | 1,877,202.43 |
| Operating profit margin | 11.72% | 11.70% |

Recent revenue is 3.09% higher than in weeks 235-247. Both 13-week
periods contain all 390 expected product-weeks. The latest observed week
is 260; its portfolio revenue growth from week 259 is -15.43%. These are
descriptive comparisons, not causal effects or a forward business target.

## Reproduce and dashboard queries

~~~powershell
.\.venv\Scripts\python.exe -m assessment_etl kpi --weeks 13 --report .\reports\kpi_summary.json
~~~

~~~sql
SELECT week_id, revenue, gross_profit_margin, operating_profit,
       revenue_growth, product_rows
FROM vw_kpi_portfolio_week
ORDER BY week_id;

SELECT product_name, week_id, revenue, gross_profit_margin,
       opening_stock_cover_days_proxy, sales_exceed_opening_inventory
FROM vw_kpi_product_week
WHERE week_id = 260
ORDER BY product_name;
~~~

The dashboard can query these views directly. It should display unavailable
credit and formal DSI metrics as unavailable with their reasons, rather than
substituting zero or fabricating customer risk scores.
