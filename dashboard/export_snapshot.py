"""Export only dashboard-facing views to a portable SQLite snapshot."""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .data import LOCAL_DATABASE, SNAPSHOT_DATABASE, VIEW_QUERIES


def export_snapshot(source: Path, target: Path) -> dict[str, int]:
    """Atomically copy KPI and latest forecast rows, excluding raw source data."""
    source = Path(source).resolve()
    target = Path(target).resolve()
    if source == target:
        raise ValueError("Source and snapshot paths must differ")
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with tempfile.NamedTemporaryFile(
        prefix="dashboard_snapshot_", suffix=".sqlite", dir=target.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as warehouse:
            with closing(sqlite3.connect(temporary_path)) as snapshot, snapshot:
                for name, query in VIEW_QUERIES.items():
                    cursor = warehouse.execute(query)
                    columns = [column[0] for column in cursor.description]
                    quoted_columns = ", ".join(f'"{column}"' for column in columns)
                    table_name = {
                        "portfolio": "vw_kpi_portfolio_week",
                        "products": "vw_kpi_product_week",
                        "forecast": "vw_latest_demand_forecast",
                    }[name]
                    # Declared types are inferred from source view metadata, including NULL-only columns.
                    metadata = warehouse.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                    types = {row[1]: (row[2] or "NUMERIC") for row in metadata}
                    definitions = ", ".join(
                        f'"{column}" {types[column]}' for column in columns
                    )
                    snapshot.execute(f'CREATE TABLE "{table_name}" ({definitions})')
                    placeholders = ", ".join("?" for _ in columns)
                    rows = cursor.fetchall()
                    snapshot.executemany(
                        f'INSERT INTO "{table_name}" ({quoted_columns}) VALUES ({placeholders})',
                        rows,
                    )
                    counts[name] = len(rows)
                snapshot.execute(
                    "CREATE INDEX idx_dashboard_product_week "
                    "ON vw_kpi_product_week(product_name, week_id)"
                )
                snapshot.execute(
                    "CREATE INDEX idx_dashboard_forecast_product_week "
                    "ON vw_latest_demand_forecast(product_name, week_id)"
                )
        temporary_path.replace(target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=LOCAL_DATABASE)
    parser.add_argument("--target", type=Path, default=SNAPSHOT_DATABASE)
    args = parser.parse_args()
    print(export_snapshot(args.source, args.target))


if __name__ == "__main__":
    main()
