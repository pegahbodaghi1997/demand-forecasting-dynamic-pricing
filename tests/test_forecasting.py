"""Forecasting checks focused on time leakage and database handoff."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np
from openpyxl import Workbook

from assessment_etl.forecasting import (
    BASELINE_METHODS,
    METHOD_GUIDANCE,
    METHOD_OBJECTIVES,
    MODEL_CONFIGS,
    Panel,
    _forest_features,
    _feature_vector,
    _metrics,
    _training_matrix,
    run_forecast,
)
from assessment_etl.pipeline import load_excel


class ForecastingTests(unittest.TestCase):
    def test_forest_product_id_is_one_hot_encoded(self) -> None:
        features = np.array([[0.0, 1.0], [1.0, 2.0]])
        prepared = _forest_features(features)
        self.assertEqual(prepared.tolist(), [[1.0, 1.0, 0.0], [2.0, 0.0, 1.0]])

    def test_features_do_not_read_future_sales_or_prices(self) -> None:
        sales = np.arange(1, 131, dtype=float).reshape(1, 130)
        prices = np.full((1, 130), 10.0)
        origin = 110
        changed_sales = sales.copy()
        changed_prices = prices.copy()
        changed_sales[:, origin:] = 100_000
        changed_prices[:, origin:] = 0.01
        first = Panel([(1, "Rice")], sales, prices, 130, "first")
        changed = Panel([(1, "Rice")], changed_sales, changed_prices, 130, "changed")
        self.assertEqual(
            _feature_vector(first, 0, origin, 26),
            _feature_vector(changed, 0, origin, 26),
        )
        original_features, original_targets = _training_matrix(first, origin, 4)
        changed_features, changed_targets = _training_matrix(changed, origin, 4)
        np.testing.assert_array_equal(original_features, changed_features)
        np.testing.assert_array_equal(original_targets, changed_targets)

    def test_zero_actuals_are_excluded_from_mape_only(self) -> None:
        result = _metrics(np.array([[0, 10]]), np.array([[5, 8]]))
        self.assertEqual(result["observations"], 2)
        self.assertEqual(result["zero_actuals"], 1)
        self.assertEqual(result["mape_positive_pct"], 20.0)
        self.assertEqual(result["mape_coverage_pct"], 50.0)

    def test_forecasts_are_versioned_and_separate_from_actuals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            workbook_path = folder / "weekly.xlsx"
            database = folder / "weekly.sqlite"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(
                [
                    "Week",
                    "Product",
                    "Sales_Units",
                    "Cost_of_Goods_Sold_COGS",
                    "Inventory_Level",
                    "Price_Per_Unit",
                ]
            )
            for product_index, product in enumerate(("Rice", "Tea")):
                for week in range(1, 131):
                    sales = 0 if week % 17 == 0 else 100 + product_index * 50 + week % 26
                    sheet.append([week, product, sales, 5 + product_index, 200, 10 + product_index])
            workbook.save(workbook_path)
            load_excel(workbook_path, database)
            result = run_forecast(database, horizon=4)
            repeated = run_forecast(database, horizon=4)
            self.assertEqual(result["backtest_origins"], [114, 118, 122, 126])
            self.assertEqual(result["forecast_rows"], 8)
            self.assertEqual(
                set(result["development_metrics"]),
                set(BASELINE_METHODS) | set(MODEL_CONFIGS),
            )
            self.assertEqual(set(result["method_guidance"]), set(METHOD_GUIDANCE))
            self.assertEqual(set(result["method_objectives"]), set(METHOD_OBJECTIVES))
            self.assertEqual(
                {name for name in MODEL_CONFIGS if name.startswith("forest_")},
                {"forest_mae", "forest_rmse", "forest_poisson"},
            )
            self.assertTrue(repeated["skipped_existing"])
            self.assertEqual(repeated["fold_metrics"], result["fold_metrics"])
            self.assertEqual(repeated["forecast_rows"], result["forecast_rows"])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*), MIN(week_id), MAX(week_id) "
                        "FROM fact_demand_forecast"
                    ).fetchone(),
                    (8, 131, 134),
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM fact_weekly_product").fetchone()[0],
                    260,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM forecast_backtest").fetchone()[0],
                    4 * 2 * 4 * (len(BASELINE_METHODS) + len(MODEL_CONFIGS)),
                )


if __name__ == "__main__":
    unittest.main()
