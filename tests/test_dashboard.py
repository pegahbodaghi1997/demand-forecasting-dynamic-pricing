"""Dashboard data and headless UI checks with synthetic pricing inputs."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from assessment_etl.forecasting import _load_panel
from assessment_etl.pipeline import load_excel
from assessment_etl.pricing import run_pricing
from dashboard.data import load_dashboard_data, period_metrics
from dashboard.export_snapshot import export_snapshot


APP_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"


def sample_database(folder: Path) -> Path:
    workbook_path = folder / "synthetic.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        ["Week", "Product", "Sales_Units", "Cost_of_Goods_Sold_COGS", "Inventory_Level", "Price_Per_Unit", "Operating_Profit"]
    )
    for week in range(1, 27):
        sheet.append([week, "Rice", 10 if week <= 13 else 20, 2, 5, 5, 10])
    workbook.save(workbook_path)
    database = folder / "warehouse.sqlite"
    load_excel(workbook_path, database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        input_sha256 = _load_panel(connection).input_sha256
        connection.execute(
            "INSERT INTO dim_week(week_id, relative_year, week_in_year) VALUES (27, 1, 27)"
        )
        connection.execute(
            """INSERT INTO forecast_run(
                run_key, input_max_week, horizon_weeks, input_sha256, feature_version,
                selected_method, selection_rule, selected_parameters_json,
                development_metrics_json, holdout_metrics_json, training_rows
            ) VALUES ('synthetic', 26, 1, ?, 'test', 'seasonal_baseline',
                'test', '{}', '{}', '{}', 26)""",
            (input_sha256,),
        )
        connection.execute(
            """INSERT INTO fact_demand_forecast(
                forecast_run_id, product_id, week_id, predicted_units,
                lower_error_band_units, upper_error_band_units
            ) VALUES (1, 1, 27, 22, 18, 26)"""
        )
    run_pricing(database)
    return database


class DashboardTests(unittest.TestCase):
    def test_snapshot_excludes_raw_data_and_preserves_forecast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = sample_database(folder)
            target = folder / "snapshot.sqlite"
            self.assertEqual(
                export_snapshot(source, target),
                {"portfolio": 26, "products": 26, "forecast": 1, "pricing": 1},
            )
            with closing(sqlite3.connect(target)) as connection:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(
                names,
                {"vw_kpi_portfolio_week", "vw_kpi_product_week", "vw_latest_demand_forecast", "vw_latest_price_recommendation"},
            )
            data = load_dashboard_data(target)
            self.assertEqual(int(data.forecast.iloc[0]["predicted_units"]), 22)
            self.assertEqual(len(data.pricing), 1)
            recent = period_metrics(data.products, last_week=26, product_count=1)
            self.assertTrue(recent["complete"])
            self.assertEqual(recent["revenue"], 1300.0)
            self.assertAlmostEqual(recent["gross_margin"], 0.6)

    def test_streamlit_renders_snapshot_without_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = sample_database(folder)
            snapshot = folder / "snapshot.sqlite"
            export_snapshot(source, snapshot)
            with patch.dict(os.environ, {"DASHBOARD_DB": str(snapshot)}):
                app = AppTest.from_file(str(APP_FILE)).run(timeout=30)
            self.assertFalse(app.exception, [item.message for item in app.exception])
            self.assertIn("Weekly commodity dashboard", app.title[0].value)
            self.assertEqual(len(app.tabs), 5)
            self.assertTrue(any("1,300.00" in item.value for item in app.metric))
            self.assertTrue(any(item.label == "Hold" and item.value == "1" for item in app.metric))
            with patch.dict(os.environ, {"DASHBOARD_DB": str(snapshot)}):
                app.sidebar.selectbox[0].set_value("Rice").run(timeout=30)
            self.assertFalse(app.exception, [item.message for item in app.exception])


if __name__ == "__main__":
    unittest.main()
