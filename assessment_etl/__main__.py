"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .pipeline import DEFAULT_DATABASE, database_summary, load_excel


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive and load weekly Excel data into SQLite")
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DATABASE, help="SQLite database file"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    load = subcommands.add_parser("load", help="Archive, clean, and load an Excel workbook")
    load.add_argument("source", type=Path, help=".xlsx or .xlsm workbook")
    subcommands.add_parser("status", help="Show table counts and ingestion runs")
    forecast = subcommands.add_parser(
        "forecast", help="Backtest methods and forecast the next 26 weeks"
    )
    forecast.add_argument(
        "--horizon", type=int, default=26, help="Forecast horizon, from 1 to 26 weeks"
    )
    forecast.add_argument(
        "--report", type=Path, help="Optional JSON path for the evaluation report"
    )
    kpi = subcommands.add_parser("kpi", help="Summarize historical business KPIs")
    kpi.add_argument(
        "--weeks", type=int, default=13, help="Length of the recent comparison period"
    )
    kpi.add_argument("--report", type=Path, help="Optional JSON path for the KPI report")
    pricing = subcommands.add_parser(
        "price", help="Create rule-based, next-week price proposals for review"
    )
    pricing.add_argument("--report", type=Path, help="Optional JSON path for price proposals")
    pricing.add_argument("--minimum-margin-pct", type=Decimal, default=Decimal("15"))
    pricing.add_argument("--maximum-change-pct", type=Decimal, default=Decimal("5"))
    pricing.add_argument("--step-pct", type=Decimal, default=Decimal("3"))
    pricing.add_argument("--demand-trigger-pct", type=Decimal, default=Decimal("20"))
    pricing.add_argument("--recent-weeks", type=int, default=13)
    args = parser.parse_args()

    if args.command == "load":
        result = load_excel(args.source, args.db)
    elif args.command == "forecast":
        from .forecasting import run_forecast

        result = run_forecast(args.db, horizon=args.horizon)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            report = {key: value for key, value in result.items() if key != "skipped_existing"}
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    elif args.command == "kpi":
        from .kpis import build_kpi_report

        result = build_kpi_report(args.db, trailing_weeks=args.weeks)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    elif args.command == "price":
        from .pricing import PricingPolicy, run_pricing

        policy = PricingPolicy(
            minimum_margin_pct=args.minimum_margin_pct,
            maximum_change_pct=args.maximum_change_pct,
            step_pct=args.step_pct,
            demand_trigger_pct=args.demand_trigger_pct,
            recent_weeks=args.recent_weeks,
        )
        result = run_pricing(args.db, policy=policy)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    else:
        result = database_summary(args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
