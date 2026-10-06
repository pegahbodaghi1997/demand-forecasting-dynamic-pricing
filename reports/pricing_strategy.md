# Rule-based next-week price proposals

## Purpose and inputs

The `price` command creates a reviewable proposal for each product in the
first week after the latest actual sales week. It reads the latest versioned
forecast, the latest unit price and unit cost, and the preceding 13 weeks of
actual sales. It does not update historical prices or write a future selling
price into the demand forecast. The forecast is a recorded-sales proxy under
the historical pricing context, not a prediction at each proposed new price.

The newest forecast must match the current cleaned sales and price history.
If source sales or prices changed after forecasting, pricing refuses to run.
Runs are versioned by the forecast, current inputs, and policy settings;
repeating an identical run does not append duplicate proposals.

## Default decision rules

All money calculations use integer cents until display. Defaults are
illustrative business settings and may be changed with the `price` command's
options. No supplied file specifies official margin or repricing limits.

1. Minimum allowed price is `ceil(unit_cost / (1 - minimum_margin))`, with
   `minimum_margin = 15%` by default. This protects *gross* unit margin.
2. A proposed price may move at most 5% from the latest observed price in one
   step. If the minimum-margin floor lies above that cap, the result is
   `review` with no recommended price. A person must resolve the conflicting
   constraints.
3. Compare the first-week forecast units with the mean actual sales of the
   preceding 13 product-weeks. A ratio of at least 1.20 triggers a 3% increase;
   at most 0.80 triggers a 3% decrease. Otherwise hold. Incomplete or zero
   recent sales make the demand signal unavailable, so the price is held unless
   the margin floor requires an increase.
4. Apply the minimum-margin floor and the maximum-change cap, then round to
   cents. The final decision is `increase`, `decrease`, `hold`, or `review`.
   Each row stores a machine-readable reason code and the policy run ID.

The latest week's opening inventory is used only for `inventory_warning`.
The flag is set if opening inventory is missing, sales are zero, sales exceed
opening inventory, or historical opening-stock cover is under seven days.
Receipts and ending stock are unknown, so this flag never automatically
changes the proposed price. It is a prompt for a human stock check.

## Break-even volume

For a changed price with positive unit gross margins, the report shows:

`ceil(forecast_units * (current_price - unit_cost) /
      (recommended_price - unit_cost))`

This is the number of units at the proposed price needed to equal the *unit
gross-margin total* implied by the reference forecast at the current price.
It is not a sales forecast at the new price and not a projected profit uplift.
If the current or proposed unit margin is nonpositive, or the price is held,
the break-even field is unavailable.

## Limits and evaluation

Observed price variation does not identify causal price elasticity by itself.
The workbook has no random price experiments, promotion calendar, competitor
prices, receipts, ending inventory, receivables, or customer default records.
Consequently, this phase does not claim optimal prices, policy uplift, formal
DSI/DSO, or credit-risk guardrails. Future inventory and unit cost are also
unknown; each proposal uses the latest observed cost and is for the next week
only. A real deployment would require current stock, an approved pricing
policy, and experimental or otherwise credible price-response evidence.

Tests check the price floor, change cap conflict, downward and upward moves,
break-even arithmetic, run idempotence, and rejection of stale forecasts.
They verify rule behavior; they cannot validate counterfactual business gain.

On the supplied workbook with the default policy, the current run proposes
11 increases and 19 holds for week 261; it proposes no decreases and no
manual-review rows. Fifteen products have a historical inventory warning.
These counts describe the configured rules on this sample, not estimated
financial improvement.
