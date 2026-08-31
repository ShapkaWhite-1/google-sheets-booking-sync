from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from booking_sync.cli import main
from booking_sync.sync import (
    google_csv_url,
    normalize_phone,
    parse_csv,
    read_csv_file,
    sync_rows,
)


FIXTURE = Path(__file__).parent / "fixtures" / "bookings.csv"


def booking_row(
    record_id: str = "ST-1",
    amount: str = "100",
    updated_at: str = "2026-08-31 10:00:00",
) -> dict[str, str]:
    return {
        "record_id": record_id,
        "property_code": "JLT-TEST-1",
        "guest_name": "Test Guest",
        "phone": "+971501234567",
        "check_in": "2026-09-10",
        "check_out": "2026-09-17",
        "amount_aed": amount,
        "status": "Confirmed",
        "responsible_manager": "Test Manager",
        "channel": "Direct",
        "updated_at": updated_at,
        "notes": "",
    }


class BookingSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "bookings.db")
        self.rows = parse_csv(read_csv_file(str(FIXTURE)))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_google_link_is_converted_to_csv_link(self) -> None:
        link = "https://docs.google.com/spreadsheets/d/table123/edit#gid=42"
        self.assertEqual(
            google_csv_url(link),
            "https://docs.google.com/spreadsheets/d/table123/export?format=csv&gid=42",
        )

    def test_uae_phone_is_normalized(self) -> None:
        self.assertEqual(normalize_phone("050 765 4321"), "+971507654321")
        self.assertEqual(normalize_phone("00971587776655"), "+971587776655")

    def test_sync_uses_newer_duplicate_and_keeps_errors_without_pii(self) -> None:
        result = sync_rows(self.rows, self.database)

        connection = sqlite3.connect(self.database)
        booking = connection.execute(
            """
            SELECT id, amount_aed_minor, notes
            FROM bookings WHERE record_id = 'ST-1001'
            """
        ).fetchone()
        error_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sync_errors)")
        }
        connection.close()

        self.assertEqual(result["valid_records"], 3)
        self.assertEqual(result["invalid_rows"], 1)
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["inserted"], 3)
        self.assertEqual(booking[1:], (425000, "Newer version"))
        self.assertIsInstance(booking[0], int)
        self.assertNotIn("row_data", error_columns)

    def test_second_run_does_not_create_duplicates(self) -> None:
        sync_rows(self.rows, self.database)
        second_result = sync_rows(self.rows, self.database)

        connection = sqlite3.connect(self.database)
        total = connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        connection.close()

        self.assertEqual(second_result["inserted"], 0)
        self.assertEqual(second_result["unchanged"], 3)
        self.assertEqual(total, 3)

    def test_invalid_newest_version_does_not_load_old_version(self) -> None:
        old = booking_row(amount="100", updated_at="2026-08-31 10:00:00")
        new_invalid = booking_row(amount="N/A", updated_at="2026-08-31 11:00:00")

        result = sync_rows([old, new_invalid], self.database)

        connection = sqlite3.connect(self.database)
        total = connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        connection.close()
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["invalid_rows"], 1)
        self.assertEqual(total, 0)

    def test_same_timestamp_with_different_data_is_conflict(self) -> None:
        first = booking_row(amount="100")
        second = booking_row(amount="200")

        result = sync_rows([first, second], self.database)

        connection = sqlite3.connect(self.database)
        total = connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        connection.close()
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(total, 0)

    def test_record_id_is_uppercase_and_case_insensitive(self) -> None:
        old = booking_row(record_id="st-1", amount="100")
        new = booking_row(
            record_id="ST-1",
            amount="200",
            updated_at="2026-08-31 11:00:00",
        )

        result = sync_rows([old, new], self.database)

        connection = sqlite3.connect(self.database)
        rows = connection.execute("SELECT record_id FROM bookings").fetchall()
        connection.close()
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(rows, [("ST-1",)])

    def test_newer_row_updates_and_older_row_is_stale(self) -> None:
        sync_rows([booking_row(amount="100")], self.database)
        updated = sync_rows(
            [booking_row(amount="200", updated_at="2026-08-31 11:00:00")],
            self.database,
        )
        stale = sync_rows(
            [booking_row(amount="50", updated_at="2026-08-31 09:00:00")],
            self.database,
        )

        connection = sqlite3.connect(self.database)
        amount = connection.execute(
            "SELECT amount_aed_minor FROM bookings WHERE record_id = 'ST-1'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(stale["stale"], 1)
        self.assertEqual(amount, 20000)

    def test_database_same_timestamp_conflict_does_not_overwrite(self) -> None:
        sync_rows([booking_row(amount="100")], self.database)
        result = sync_rows([booking_row(amount="200")], self.database)

        connection = sqlite3.connect(self.database)
        amount = connection.execute(
            "SELECT amount_aed_minor FROM bookings WHERE record_id = 'ST-1'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(amount, 10000)

    def test_cli_has_partial_success_exit_code(self) -> None:
        code = main(
            ["--csv-file", str(FIXTURE), "--database", self.database]
        )
        allowed_code = main(
            [
                "--csv-file",
                str(FIXTURE),
                "--database",
                self.database,
                "--allow-errors",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(allowed_code, 0)


if __name__ == "__main__":
    unittest.main()
