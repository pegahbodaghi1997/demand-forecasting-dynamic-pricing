"""Reproducible KPI summaries over the cleaned weekly fact table."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .pipeline import DEFAULT_DATABASE, connect


KPI_AVAILABILITY = {
    "gross_profit_margin": {
        "status": "calculated",
        "reason": "Gross profit divided by revenue, using sums for aggregated margins.",
    },
    "revenue_growth": {
        "status": "calculated",
        "reason": "Compared with the immediately preceding week or complete equal-length period.",
    },
    "operating_profit": {
        "status": "source_reported",
        "reason": "Operating expenses are absent; an incomplete group has no total.",
    },
    "inventory_turnover_ratio": {
        "status": "source_unverified",
        "reason": "The workbook does not define its calculation period or inventory basis.",
    },
    "opening_stock_cover_days_proxy": {
        "status": "proxy",
        "reason": "Seven times opening units divided by observed sales units for that week.",
    },
    "days_sales_of_inventory": {
        "status": "unavailable",
        "reason": "Average inventory, replenishment timing, and a defined turnover period are absent.",
    },
    "days_sales_outstanding": {
        "status": "unavailable",
        "reason": "Receivables balances and payment dates are absent.",
    },
    "customer_default_risk_rating": {
        "status": "unavailable",
        "reason": "Customer identities, credit exposure, and repayment outcomes are absent.",
    },
}


def _money(cents: int | None) -> float | None:
    return round(cents / 100, 2) if cents is not None else None


def _percentage(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(100 * numerator / denominator, 2)


def _period_summary(
    connection: sqlite3.Connection,
    start_week: int,
    end_week: int,
    product_count: int,
) -> dict[str, object]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS product_week_rows,
               COUNT(DISTINCT week_id) AS observed_week_count,
               COALESCE(SUM(sales_units), 0) AS sales_units,
               COALESCE(SUM(revenue_cents), 0) AS revenue_cents,
               COALESCE(SUM(sales_units * unit_cost_cents), 0) AS cogs_cents,
               COALESCE(SUM(gross_profit_cents), 0) AS gross_profit_cents,
               SUM(operating_profit_cents) AS available_operating_profit_cents,
               SUM(CASE WHEN operating_profit_cents IS NOT NULL THEN 1 ELSE 0 END)
                   AS operating_profit_rows,
               SUM(CASE WHEN opening_inventory_units IS NOT NULL THEN 1 ELSE 0 END)
                   AS inventory_rows,
               COALESCE(SUM(sales_recovered), 0) AS recovered_sales_rows
        FROM fact_weekly_product
        WHERE week_id BETWEEN ? AND ?
        """,
        (start_week, end_week),
    ).fetchone()
    expected_weeks = end_week - start_week + 1
    expected_rows = product_count * expected_weeks
    actual_rows = row["product_week_rows"]
    operating_rows = row["operating_profit_rows"] or 0
    all_operating_available = actual_rows > 0 and operating_rows == actual_rows
    operating_cents = (
        row["available_operating_profit_cents"] if all_operating_available else None
    )
    return {
        "start_week": start_week,
        "end_week": end_week,
        "expected_week_count": expected_weeks,
        "observed_week_count": row["observed_week_count"],
        "product_week_rows": actual_rows,
        "expected_product_week_rows": expected_rows,
        "complete_panel": actual_rows == expected_rows,
        "sales_units": row["sales_units"],
        "revenue": _money(row["revenue_cents"]),
        "cost_of_goods_sold": _money(row["cogs_cents"]),
        "gross_profit": _money(row["gross_profit_cents"]),
        "gross_profit_margin_pct": _percentage(
            row["gross_profit_cents"], row["revenue_cents"]
        ),
        "operating_profit": _money(operating_cents),
        "operating_profit_margin_pct": _percentage(
            operating_cents, row["revenue_cents"]
        ),
        "operating_profit_rows": operating_rows,
        "operating_profit_coverage_pct": _percentage(operating_rows, actual_rows),
        "inventory_rows": row["inventory_rows"] or 0,
        "inventory_coverage_pct": _percentage(row["inventory_rows"] or 0, actual_rows),
        "recovered_sales_rows": row["recovered_sales_rows"],
    }


def build_kpi_report(
    database: Path = DEFAULT_DATABASE,
    *,
    trailing_weeks: int = 13,
) -> dict[str, object]:
    """Summarize historical KPIs without treating forecast weeks as actuals."""
    if trailing_weeks < 1:
        raise ValueError("trailing_weeks must be positive")
    connection = connect(database)
    try:
        minimum_week, maximum_week = connection.execute(
            "SELECT MIN(week_id), MAX(week_id) FROM fact_weekly_product"
        ).fetchone()
        if maximum_week is None:
            raise ValueError("No cleaned historical sales data are available")
        product_count = connection.execute(
            "SELECT COUNT(*) FROM dim_product"
        ).fetchone()[0]
        recent_start = max(1, maximum_week - trailing_weeks + 1)
        history = _period_summary(connection, minimum_week, maximum_week, product_count)
        recent = _period_summary(connection, recent_start, maximum_week, product_count)
        previous = (
            _period_summary(
                connection,
                recent_start - trailing_weeks,
                recent_start - 1,
                product_count,
            )
            if recent_start > trailing_weeks
            else None
        )
        comparable = bool(
            previous is not None
            and recent["complete_panel"]
            and previous["complete_panel"]
        )
        growth = (
            round(
                100 * (recent["revenue"] - previous["revenue"])
                / previous["revenue"],
                2,
            )
            if comparable and previous["revenue"] > 0
            else None
        )
        latest = dict(
            connection.execute(
                "SELECT * FROM vw_kpi_portfolio_week WHERE week_id = ?",
                (maximum_week,),
            ).fetchone()
        )
        low_cover = [
            {
                **dict(row),
                "opening_stock_cover_days_proxy": round(
                    row["opening_stock_cover_days_proxy"], 2
                ),
            }
            for row in connection.execute(
                """
                SELECT product_name, sales_units, opening_inventory_units,
                       opening_stock_cover_days_proxy,
                       sales_exceed_opening_inventory
                FROM vw_kpi_product_week
                WHERE week_id = ? AND opening_stock_cover_days_proxy IS NOT NULL
                ORDER BY opening_stock_cover_days_proxy, product_name
                LIMIT 5
                """,
                (maximum_week,),
            )
        ]
        top_products = []
        for row in connection.execute(
            """
            SELECT p.product_name, COUNT(*) AS observed_weeks,
                   SUM(f.sales_units) AS sales_units,
                   SUM(f.revenue_cents) AS revenue_cents,
                   SUM(f.gross_profit_cents) AS gross_profit_cents
            FROM fact_weekly_product AS f
            JOIN dim_product AS p ON p.product_id = f.product_id
            WHERE f.week_id BETWEEN ? AND ?
            GROUP BY f.product_id
            ORDER BY revenue_cents DESC, p.product_name
            LIMIT 5
            """,
            (recent_start, maximum_week),
        ):
            top_products.append(
                {
                    "product_name": row["product_name"],
                    "observed_weeks": row["observed_weeks"],
                    "sales_units": row["sales_units"],
                    "revenue": _money(row["revenue_cents"]),
                    "gross_profit_margin_pct": _percentage(
                        row["gross_profit_cents"], row["revenue_cents"]
                    ),
                }
            )
        return {
            "max_observed_week": maximum_week,
            "product_count": product_count,
            "all_history": history,
            "recent_period": recent,
            "previous_equal_length_period": previous,
            "recent_revenue_growth_vs_previous_pct": growth,
            "latest_week": latest,
            "lowest_latest_opening_stock_cover": low_cover,
            "top_recent_products_by_revenue": top_products,
            "kpi_availability": KPI_AVAILABILITY,
        }
    finally:
        connection.close()
