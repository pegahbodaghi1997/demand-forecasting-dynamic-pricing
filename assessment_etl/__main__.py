"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
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
    args = parser.parse_args()

    result = (
        load_excel(args.source, args.db)
        if args.command == "load"
        else database_summary(args.db)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
