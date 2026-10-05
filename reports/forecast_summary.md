# Demand forecasting: evaluation and handoff

## Scope and data

The target is observed weekly Sales_Units for 30 products. The cleaned fact
table supplies 260 complete weeks per product. These are recorded sales,
not unconstrained demand: the workbook has no lost-sales or in-week receipt
records. The 16 recovered historical sales values are included, with their
provenance flags in the fact table.

The workbook has no real calendar dates. A 52-week cycle is used as a
relative seasonal feature. Future price and inventory paths are unknown.
The model uses only prices known at the forecast origin and does not use
future inventory or the incomplete historical Demand_Forecast column.

## Time-aware protocol

| Fold | Training ends at week | Test weeks | Purpose |
| --- | ---: | --- | --- |
| 1 | 156 | 157-182 | Method selection |
| 2 | 182 | 183-208 | Method selection |
| 3 | 208 | 209-234 | Method selection |
| 4 | 234 | 235-260 | Final holdout report |

Each method predicts 26 weeks at once using only values available at the
training origin. The global gradient-boosted model uses product identity,
horizon, 52-week seasonal phase, known sales lags and rolling statistics,
sales at the comparable week a year earlier, and last known price signals.
The two learned candidates have the same tree structure and differ in
absolute-error versus squared-error loss (160 iterations, learning rate
0.06, 15 leaves, minimum 30 samples per leaf, regularization 1.0).

Selection uses pooled MAE over the first three folds. A simpler baseline
would win if its MAE were within 1% of the best learned model. The fourth
fold is not used to choose a method.

## Results

| Method | Development MAE | Development RMSE | Holdout MAE | Holdout RMSE | Holdout WAPE |
| --- | ---: | ---: | ---: | ---: | ---: |
| Repeat week from 52 weeks ago | 743.936 | 1117.092 | 610.599 | 946.054 | 49.190% |
| Mean of latest 13 weeks | 693.037 | 859.507 | 562.025 | 738.843 | 45.276% |
| Global GBDT, absolute-error loss | **640.349** | 895.577 | **456.876** | 710.635 | **36.806%** |
| Global GBDT, squared-error loss | 660.732 | 894.993 | 491.023 | **687.845** | 39.557% |

The absolute-error GBDT was selected before examining the holdout. Its
holdout MAE is 18.7% lower than the best deployable baseline. The squared-error
variant has a slightly better holdout RMSE, but it was not the selected method
under the declared MAE rule.

Holdout MAPE on positive-sales observations is 90.351%. Only 86.79% of
holdout observations have positive actual sales; MAPE is undefined for the
remaining zero-sales observations. WAPE and MAE include all 780 holdout
product-weeks. The selected model has a positive holdout bias of 156.063
units per product-week, so its output should not be presented as precise
unconstrained demand.

The workbook's historical Demand_Forecast is a reference only. It is absent
for 64 of the 780 holdout product-weeks and may not be available at a real
forecast origin. On the 716 holdout rows where it exists, its MAE is 651.855;
the selected model's MAE on those exact same rows is 448.132. This reference
was not eligible for method selection because its future values are unavailable.

## Forecast artifact

The final model is retrained through week 260 and writes 780 rows (30
products x 26 weeks) for weeks 261-286 into fact_demand_forecast. Backtest
predictions for every method are stored in forecast_backtest. The
vw_latest_demand_forecast view exposes the newest run for the dashboard.
The point forecast totals 1,106,441 units across all products and future
weeks; it is not a validated sales target.

Each forecast has a descriptive error band based on the 90th percentile of
absolute development-fold errors at its horizon. These are broad ranges,
not calibrated probabilities. On the final historical fold, 91.92% of
actual sales values fall within those bands. The median band half-width is
about 1,536 units. See forecast_evaluation.json for per-fold metrics and
the five products with the largest holdout MAE.

## Reproduce

From the project root:

~~~powershell
.\.venv\Scripts\python.exe -m assessment_etl forecast --horizon 26 --report .\reports\forecast_evaluation.json
.\.venv\Scripts\python.exe -m assessment_etl status
~~~

The model run is keyed by the cleaned input hash, horizon, feature version,
hyperparameters, and scikit-learn version. Repeating the same run does not
append duplicate forecasts. New or revised clean data produce a new
versioned run.
