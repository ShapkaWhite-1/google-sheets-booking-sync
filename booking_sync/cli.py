from __future__ import annotations

import argparse
import json
import sys

from booking_sync.sync import download_sheet, parse_csv, read_csv_file, sync_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load bookings from Google Sheets into SQLite"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet-url", help="Google Spreadsheet URL")
    source.add_argument("--csv-file", help="Path to a local CSV file")
    parser.add_argument(
        "--database",
        default="data/bookings.db",
        help="SQLite file path (default: data/bookings.db)",
    )
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="Return exit code 0 when individual rows are rejected",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.sheet_url:
            csv_text = download_sheet(args.sheet_url)
        else:
            csv_text = read_csv_file(args.csv_file)
        summary = sync_rows(parse_csv(csv_text), args.database)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if (summary["invalid_rows"] or summary["conflicts"]) and not args.allow_errors:
            return 2
        return 0
    except Exception as exc:
        print(f"Sync error: {exc}", file=sys.stderr)
        return 1
