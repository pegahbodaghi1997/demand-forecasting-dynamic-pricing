"""KPI calculations protect weighted margins and missing-data meaning."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook

from assessment_etl.kpis import build_kpi_report
from assessment_etl.pipeline import load_excel


def write_trade_workbook(path: Path, rows: list[list[object]]) -> None:
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
            "Operating_Profit",
        ]
    )
    for row in rows:
        sheet.append(row)
    workbook.save(path)


class KpiTests(unittest.TestCase):
    def test_weighted_margin_missing_operating_profit_and_week_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            database = folder / "trade.sqlite"
            first = folder / "first.xlsx"
            write_trade_workbook(
                first,
                [
                    [1, "Rice", 10, 6, 20, 10, 30],
                    [1, "Tea", 2, 1, 5, 5, None],
                    [2, "Rice", 0, 6, 20, 10, None],
                    [2, "Tea", 2, 1, 0, 5, 4],
                ],
            )
            load_excel(first, database)
            with closing(sqlite3.connect(database)) as connection:
                rice = connection.execute(
                    """
                    SELECT cost_of_goods_sold, gross_profit_margin,
                           opening_stock_cover_days_proxy
                    FROM vw_kpi_product_week
                    WHERE product_name = 'Rice' AND week_id = 1
                    """
                ).fetchone()
                self.assertEqual(rice[0], 60.0)
                self.assertAlmostEqual(rice[1], 0.4)
                self.assertEqual(rice[2], 14.0)
                first_week = connection.execute(
                    """
                    SELECT revenue, gross_profit_margin, operating_profit,
                           operating_profit_rows
                    FROM vw_kpi_portfolio_week WHERE week_id = 1
                    """
                ).fetchone()
                self.assertEqual(first_week[0], 110.0)
                self.assertAlmostEqual(first_week[1], 48 / 110)
                self.assertIsNone(first_week[2])
                self.assertEqual(first_week[3], 1)
                second_week = connection.execute(
                    """
                    SELECT revenue_growth, gross_profit_margin
                    FROM vw_kpi_portfolio_week WHERE week_id = 2
                    """
                ).fetchone()
                self.assertAlmostEqual(second_week[0], -100 / 110)
                self.assertAlmostEqual(second_week[1], 0.8)
                rice_zero = connection.execute(
                    """
                    SELECT gross_profit_margin, opening_stock_cover_days_proxy,
                           revenue_growth
                    FROM vw_kpi_product_week
                    WHERE product_name = 'Rice' AND week_id = 2
                    """
                ).fetchone()
                self.assertIsNone(rice_zero[0])
                self.assertIsNone(rice_zero[1])
                self.assertEqual(rice_zero[2], -1.0)

            report = build_kpi_report(database, trailing_weeks=1)
            self.assertEqual(report["max_observed_week"], 2)
            self.assertEqual(report["recent_revenue_growth_vs_previous_pct"], -90.91)
            self.assertEqual(report["all_history"]["gross_profit_margin_pct"], 46.67)
            self.assertIsNone(report["all_history"]["operating_profit"])
            self.assertEqual(
                report["kpi_availability"]["days_sales_outstanding"]["status"],
                "unavailable",
            )

            later = folder / "later.xlsx"
            write_trade_workbook(
                later,
                [[4, "Rice", 2, 6, 1, 10, 6], [4, "Tea", 0, 1, 5, 5, None]],
            )
            load_excel(later, database)
            report = build_kpi_report(database, trailing_weeks=1)
            self.assertEqual(report["max_observed_week"], 4)
            self.assertIsNone(report["recent_revenue_growth_vs_previous_pct"])
            self.assertFalse(report["all_history"]["complete_panel"])
            self.assertEqual(
                report["lowest_latest_opening_stock_cover"][0][
                    "opening_stock_cover_days_proxy"
                ],
                3.5,
            )
            with closing(sqlite3.connect(database)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT revenue_growth FROM vw_kpi_portfolio_week WHERE week_id = 4"
                    ).fetchone()[0]
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT revenue_growth FROM vw_kpi_product_week "
                        "WHERE product_name = 'Rice' AND week_id = 4"
                    ).fetchone()[0]
                )


if __name__ == "__main__":
    unittest.main()
