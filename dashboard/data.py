"""Load dashboard data without changing the operational SQLite database."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DATABASE = PROJECT_ROOT / "data" / "assessment.sqlite"
SNAPSHOT_DATABASE = PROJECT_ROOT / "dashboard" / "data" / "assessment_snapshot.sqlite"

VIEW_QUERIES = {
    "portfolio": "SELECT * FROM vw_kpi_portfolio_week ORDER BY week_id",
    "products": "SELECT * FROM vw_kpi_product_week ORDER BY product_name, week_id",
    "forecast": "SELECT * FROM vw_latest_demand_forecast ORDER BY product_name, week_id",
    "pricing": "SELECT * FROM vw_latest_price_recommendation ORDER BY product_name",
}


@dataclass(frozen=True)
class DashboardData:
    portfolio: pd.DataFrame
    products: pd.DataFrame
    forecast: pd.DataFrame
    pricing: pd.DataFrame


def database_path() -> Path:
    """Use an explicit path, then the local warehouse, then a portable snapshot."""
    configured = os.environ.get("DASHBOARD_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    if LOCAL_DATABASE.is_file():
        return LOCAL_DATABASE
    return SNAPSHOT_DATABASE


def load_dashboard_data(path: Path) -> DashboardData:
    """Read only the published view contract; never create a missing database."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Dashboard database not found: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        tables = {}
        for name, query in VIEW_QUERIES.items():
            try:
                tables[name] = pd.read_sql_query(query, connection)
            except pd.errors.DatabaseError:
                if name != "pricing":
                    raise
                # Snapshots made before the pricing phase remain readable.
                tables[name] = pd.DataFrame()
    except (sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
        raise ValueError(f"Dashboard database is missing a required view or table: {exc}") from exc
    finally:
        connection.close()
    return DashboardData(**tables)


def period_metrics(
    rows: pd.DataFrame,
    *,
    last_week: int,
    product_count: int,
    weeks: int = 13,
) -> dict[str, float | int | bool | None]:
    """Calculate a weighted, coverage-aware summary of observed sales."""
    first_week = last_week - weeks + 1
    period = rows.loc[rows["week_id"].between(first_week, last_week)]
    revenue = float(period["revenue"].sum())
    gross_profit = float(period["gross_profit"].sum())
    operating_complete = len(period) > 0 and period["operating_profit"].notna().all()
    return {
        "first_week": first_week,
        "last_week": last_week,
        "rows": len(period),
        "expected_rows": product_count * weeks,
        "complete": len(period) == product_count * weeks,
        "sales_units": int(period["sales_units"].sum()),
        "revenue": revenue,
        "gross_profit": gross_profit,
        "gross_margin": gross_profit / revenue if revenue > 0 else None,
        "operating_profit": float(period["operating_profit"].sum()) if operating_complete else None,
    }
