# Weekly sales forecasting: model comparison

## Data and validation

The target is recorded weekly Sales_Units for 30 products, with 260 complete
weeks per product. Recorded sales are only a proxy for unconstrained demand:
the workbook does not contain lost sales or in-week receipts. Sixteen historical
sales values recovered by the ingestion rules remain flagged in the fact table.
The workbook has no calendar dates, so the 52-week seasonal cycle is relative.
Future prices and inventory are unavailable. Models use only information known
at each forecast origin.

| Window | Training ends | Predicted weeks | Role |
| --- | ---: | --- | --- |
| 1 | 156 | 157-182 | Method selection |
| 2 | 182 | 183-208 | Method selection |
| 3 | 208 | 209-234 | Method selection |
| 4 | 234 | 235-260 | Retrospective check |

Each candidate predicts 26 weeks directly, using the same origins, target
rows, and available features. The first three windows select a method by
pooled MAE. A baseline wins only when its MAE is within 1% of the best learned
model. Window 4 does not enter that selection calculation. It was inspected in
an earlier project iteration, so it is not a fresh untouched holdout for this
expanded comparison. A new future period is needed to confirm model choice.

The histogram gradient boosting models use product identity, target horizon,
relative seasonal phase, historical sales lags and rolling summaries, the
comparable week 52 weeks earlier, and price signals known at the origin.
They share 160 iterations, learning rate 0.06, 15 leaves, minimum 30 samples
per leaf, and L2 regularization 1.0. Their losses are absolute error, squared
error, Poisson, and quantile at 0.25 and 0.75. Gamma was excluded because it
requires strictly positive targets and this data contains zero-sales weeks.
Quantile 0.50 duplicates the median-oriented absolute-error objective.

Random forest is a separate bagged-tree family. It sees the same information,
with one-hot product identity. Its three variants use absolute error, squared
error, and Poisson **split criteria**, respectively. These criteria determine
tree splits and terminal values; they are not a global boosting loss. Each
forest has 48 trees, maximum depth 12, minimum 20 samples per leaf, 80% of
features considered per split, a 25% bootstrap sample per tree, and a fixed
random seed. No candidate received a data-driven hyperparameter search.
The objective and criterion names follow the scikit-learn documentation for
[histogram gradient boosting](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingRegressor.html)
and [random forest](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestRegressor.html).

## How to read the metrics

Lower is better for all four reported error measures. MAE is mean absolute
error in sales units. RMSE is in sales units and penalizes large misses more.
WAPE is total absolute error divided by total observed sales; it includes
zero-sales rows. MAPE is the mean percentage error only where actual sales
are positive; it is undefined for zero-sales rows. Since every method uses
the same evaluation rows, MAE and WAPE have the same ranking within a window.

The first three windows contain 2,340 product-weeks, including 431 zeros;
MAPE covers 81.58% of rows. Window 4 contains 780 product-weeks, including
103 zeros; MAPE covers 86.79%. MAPE can overemphasize small positive sales
and cannot describe the omitted zero-sales rows.

## Development results and decision guidance

These are the metrics used to select the deployed method. Decision guidance
assumes the validation result remains competitive for the business objective.

| Method | MAE | RMSE | WAPE | MAPE on positive sales | When to consider it |
| --- | ---: | ---: | ---: | ---: | --- |
| Week from 52 weeks earlier | 743.936 | 1117.092 | 58.356% | 62.440% | Seasonal repetition and a transparent rule matter most. |
| Mean of latest 13 weeks | 693.037 | 859.507 | 54.364% | 72.837% | Recent level shifts and operational simplicity matter most. |
| Gradient boosting, absolute error | 640.349 | 895.577 | 50.231% | 65.549% | Typical absolute unit error matters more than rare large misses. |
| Gradient boosting, squared error | 660.732 | 894.993 | 51.830% | 68.682% | Large misses in either direction carry extra cost. |
| Gradient boosting, Poisson | 663.952 | 888.081 | 52.082% | 69.034% | A nonnegative count-demand objective is wanted and measured error is acceptable. |
| Gradient boosting, 25th quantile | 790.841 | 1038.689 | 62.036% | 67.093% | Excess stock is much costlier than shortages; expect lower forecasts. |
| Gradient boosting, 75th quantile | 672.553 | 995.213 | 52.757% | 75.601% | Stockouts are much costlier than excess stock; expect higher forecasts. |
| Random forest, absolute-error criterion | 608.135 | 863.611 | 47.704% | 67.521% | A bagged-tree median-oriented forecast is wanted and typical error matters. |
| Random forest, squared-error criterion | **605.651** | **813.953** | **47.509%** | **60.108%** | Prefer the strongest development accuracy across all four reported measures. |
| Random forest, Poisson criterion | 613.103 | 814.190 | 48.093% | 60.666% | A count-oriented bagged-tree forecast is wanted and accuracy is close. |

The selected random forest with squared-error criterion improves development
MAE by 5.4% relative to the best gradient boosting candidate (605.651 versus
640.349). This is evidence for this data and fixed configuration, not a claim
that random forests are generally better. The 25th and 75th quantile models
are decision-oriented alternatives: their higher MAE can be acceptable if
the business has asymmetric shortage and holding costs. A cost-based choice
requires those costs to be specified and evaluated directly.

## Retrospective final window

These results are useful diagnostics but cannot be used as a new independent
test of the expanded search, because this period was inspected previously.

| Method | MAE | RMSE | WAPE | MAPE on positive sales | Bias, units per product-week |
| --- | ---: | ---: | ---: | ---: | ---: |
| Week from 52 weeks earlier | 610.599 | 946.054 | 49.190% | 111.404% | -44.478 |
| Mean of latest 13 weeks | 562.025 | 738.843 | 45.276% | 96.820% | +83.292 |
| Gradient boosting, absolute error | 456.876 | 710.635 | 36.806% | 90.351% | +156.063 |
| Gradient boosting, squared error | 491.023 | **687.845** | 39.557% | 89.006% | +61.661 |
| Gradient boosting, Poisson | 496.731 | 690.062 | 40.016% | 86.158% | +30.002 |
| Gradient boosting, 25th quantile | 644.637 | 849.721 | 51.932% | **78.241%** | -405.450 |
| Gradient boosting, 75th quantile | 533.213 | 860.624 | 42.955% | 103.660% | +410.635 |
| Random forest, absolute-error criterion | **452.517** | 702.245 | **36.455%** | 89.177% | +118.468 |
| Random forest, squared-error criterion | 488.001 | 691.378 | 39.313% | 85.098% | +30.691 |
| Random forest, Poisson criterion | 496.634 | 690.426 | 40.009% | 85.825% | +11.417 |

The selected forest has a higher retrospective MAE than both the forest with
absolute-error criterion and the absolute-error gradient booster. Switching
to either one because of this already inspected period would invalidate the
selection protocol. The lowest retrospective MAPE belongs to the 25th
quantile model, which underpredicts by about 405 units per product-week;
that metric alone is not a sound reason to choose it.

## Stored forecast and limitations

The selected forest is retrained through week 260 and writes 780 predictions
for weeks 261-286 to fact_demand_forecast. The local run is ID 2; the earlier
run remains in the database, and vw_latest_demand_forecast selects the newest
run. The new point forecasts total 1,019,745 units. This is a forecast of
recorded sales, not a validated unconstrained demand target.

All 31,200 backtest predictions for the ten methods are stored in
forecast_backtest. Error bands use the 90th percentile of absolute development
errors at each horizon. They are descriptive, not calibrated probabilities.
For the retrospective final window, 94.10% of actuals fall in those bands;
the median half-width is 1,345 units. The historical Demand_Forecast column
is incomplete and may not be available at a forecast origin, so it is a
reference only and never a candidate model.

The machine-readable details, including metrics by fold, method objectives,
decision guidance, and the worst five products for the selected model, are
in forecast_evaluation.json.

## Reproduce

From the project root:

~~~powershell
.\.venv\Scripts\python.exe -m assessment_etl forecast --horizon 26 --report .\reports\forecast_evaluation.json
.\.venv\Scripts\python.exe -m assessment_etl status
~~~

Runs are keyed by the cleaned input hash, horizon, feature version, model
configuration, and scikit-learn version. Repeating an identical run does not
append duplicate forecasts. New clean data or configurations produce a new
versioned run.
