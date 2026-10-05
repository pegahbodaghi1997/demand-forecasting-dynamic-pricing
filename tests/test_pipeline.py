"""Integration checks for raw preservation and generic Excel handling."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook

from assessment_etl.pipeline import PROJECT_ROOT, database_summary, load_excel


def write_workbook(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Weekly trade"
    sheet.append(["Export generated for a different workbook"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


class PipelineIntegrationTests(unittest.TestCase):
    def test_sample_archive_clean_load_and_idempotence(self) -> None:
        source = PROJECT_ROOT / "Technical_Assessment" / "Sample_Data.xlsx"
        if not source.exists():
            self.skipTest("Assessment sample workbook is not present")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sample.sqlite"
            first = load_excel(source, database)
            second = load_excel(source, database)
            summary = database_summary(database)
            self.assertEqual((first["raw_rows"], first["accepted_rows"]), (7839, 7800))
            self.assertEqual(first["duplicate_rows"], 39)
            self.assertTrue(second["skipped_existing"])
            self.assertEqual(summary["counts"]["ingestion_run"], 1)
            self.assertEqual(summary["counts"]["raw_weekly"], 7839)
            self.assertEqual(summary["counts"]["fact_weekly_product"], 7800)
            self.assertEqual(summary["counts"]["dim_product"], 30)
            self.assertEqual(summary["counts"]["dim_week"], 260)
            self.assertEqual(summary["checks"]["foreign_key_violations"], 0)
            with closing(sqlite3.connect(database)) as connection:
                archived = connection.execute(
                    "SELECT source_workbook FROM ingestion_run"
                ).fetchone()[0]
                self.assertEqual(archived, source.read_bytes())
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM fact_weekly_product WHERE sales_recovered = 1"
                    ).fetchone()[0],
                    16,
                )

    def test_reordered_headers_extra_column_bad_rows_and_later_update(self) -> None:
        header = [
            "PRODUCT",
            "Week",
            "Unit Price",
            "Unit Cost",
            "Sales Units",
            "Revenue",
            "Gross Profit",
            "Inventory Level",
            "Campaign",
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            database = folder / "trade.sqlite"
            first_file = folder / "first.xlsx"
            write_workbook(
                first_file,
                [
                    header,
                    ["Rice", 1, 10, 6, None, 100, 40, 20, "spring"],
                    ["Rice", 1, 10, 6, None, 100, 40, 20, "spring"],
                    ["Rice", 2, 10, 6, 12, 120, 48, 0, "summer"],
                    ["Rice", 3, 10, 6, "bad", None, None, 20, None],
                    ["Rice", 4, 10, 6, 1, 10, 4, 20, None],
                    ["Rice", 4, 10, 6, 2, 20, 8, 20, None],
                ],
            )
            result = load_excel(first_file, database)
            self.assertEqual(
                (
                    result["raw_rows"],
                    result["accepted_rows"],
                    result["rejected_rows"],
                    result["duplicate_rows"],
                ),
                (6, 2, 3, 1),
            )
            self.assertEqual(result["issues"]["duplicate_conflict"], 2)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT sales_units FROM fact_weekly_product WHERE week_id = 1"
                    ).fetchone()[0],
                    10,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT extra_cells_json FROM raw_weekly WHERE excel_row = 3"
                    ).fetchone()[0],
                    '{"Campaign": "spring"}',
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM fact_weekly_product WHERE week_id = 4"
                    ).fetchone()[0],
                    0,
                )

            second_file = folder / "second.xlsx"
            write_workbook(
                second_file,
                [header, ["Rice", 2, 10, 6, 20, 200, 80, 5, "revision"]],
            )
            load_excel(second_file, database)
            summary = database_summary(database)
            self.assertEqual(summary["counts"]["ingestion_run"], 2)
            self.assertEqual(summary["counts"]["raw_weekly"], 7)
            self.assertEqual(summary["counts"]["fact_weekly_product"], 2)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT sales_units FROM fact_weekly_product WHERE week_id = 2"
                    ).fetchone()[0],
                    20,
                )

    def test_unreadable_workbook_remains_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "broken.xlsx"
            database = folder / "broken.sqlite"
            source.write_bytes(b"not an Excel workbook")
            with self.assertRaises(Exception):
                load_excel(source, database)
            with closing(sqlite3.connect(database)) as connection:
                archived, status = connection.execute(
                    "SELECT source_workbook, status FROM ingestion_run"
                ).fetchone()
                self.assertEqual(archived, source.read_bytes())
                self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
