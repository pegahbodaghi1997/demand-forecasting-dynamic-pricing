"""Price proposals respect economic limits and forecast provenance."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from assessment_etl.forecasting import _load_panel
from assessment_etl.pipeline import load_excel
from assessment_etl.pricing import PricingPolicy, propose_price, run_pricing


def pricing_database(folder: Path) -> Path:
    workbook_path = folder / "prices.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Week", "Product", "Sales_Units", "Cost_of_Goods_Sold_COGS", "Inventory_Level", "Price_Per_Unit"])
    for week in range(1, 27):
        sheet.append([week, "Rice", 10, 6, 5, 10])
        sheet.append([week, "Tea", 20, 15, 50, 20])
    workbook.save(workbook_path)
    database = folder / "prices.sqlite"
    load_excel(workbook_path, database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        fingerprint = _load_panel(connection).input_sha256
        connection.execute("INSERT INTO dim_week VALUES (27, 1, 27)")
        connection.execute(
            """INSERT INTO forecast_run(
                run_key, input_max_week, horizon_weeks, input_sha256,
                feature_version, selected_method, selection_rule,
                selected_parameters_json, development_metrics_json,
                holdout_metrics_json, training_rows
            ) VALUES ('test-pricing', 26, 1, ?, 'test', 'test-method',
                'test', '{}', '{}', '{}', 52)""",
            (fingerprint,),
        )
        product_ids = dict(connection.execute("SELECT product_name, product_id FROM dim_product"))
        connection.executemany(
            """INSERT INTO fact_demand_forecast VALUES (?, ?, ?, ?, ?, ?)""",
            [(1, product_ids["Rice"], 27, 15, 10, 20),
             (1, product_ids["Tea"], 27, 10, 5, 15)],
        )
    return database


class PricingTests(unittest.TestCase):
    def test_floor_cap_and_break_even_math(self) -> None:
        proposal = propose_price(
            current_price_cents=1000,
            unit_cost_cents=600,
            forecast_units=15,
            recent_sales_sum=130,
            recent_sales_count=13,
            policy=PricingPolicy(),
        )
        self.assertEqual(proposal["recommended_price_cents"], 1030)
        self.assertEqual(proposal["decision"], "increase")
        self.assertEqual(proposal["break_even_units"], 14)
        decrease = propose_price(
            current_price_cents=2000,
            unit_cost_cents=1500,
            forecast_units=10,
            recent_sales_sum=260,
            recent_sales_count=13,
            policy=PricingPolicy(),
        )
        self.assertEqual(decrease["recommended_price_cents"], 1940)
        self.assertEqual(decrease["decision"], "decrease")
        self.assertEqual(decrease["break_even_units"], 12)
        review = propose_price(
            current_price_cents=1000,
            unit_cost_cents=900,
            forecast_units=15,
            recent_sales_sum=130,
            recent_sales_count=13,
            policy=PricingPolicy(minimum_margin_pct=Decimal("20")),
        )
        self.assertEqual(review["decision"], "review")
        self.assertIsNone(review["recommended_price_cents"])
        self.assertEqual(review["demand_signal"], "high")
        self.assertEqual(review["reason_code"], "margin_floor_exceeds_change_cap")

    def test_persistence_idempotence_and_stale_forecast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = pricing_database(Path(directory))
            first = run_pricing(database)
            second = run_pricing(database)
            self.assertEqual(first["decision_counts"], {"increase": 1, "decrease": 1, "hold": 0, "review": 0})
            self.assertTrue(second["skipped_existing"])
            self.assertEqual(first["pricing_run_id"], second["pricing_run_id"])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pricing_run").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM vw_latest_price_recommendation").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM fact_weekly_product").fetchone()[0], 52)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE fact_weekly_product SET sales_units = 11 WHERE product_id = 1 AND week_id = 26")
            with self.assertRaisesRegex(ValueError, "forecast is stale"):
                run_pricing(database)


if __name__ == "__main__":
    unittest.main()
