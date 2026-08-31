from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


EXPECTED_COLUMNS = {
    "record_id",
    "property_code",
    "guest_name",
    "phone",
    "check_in",
    "check_out",
    "amount_aed",
    "status",
    "responsible_manager",
    "channel",
    "updated_at",
    "notes",
}

DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
    "%d-%b-%Y",
)

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

STATUS_NAMES = {
    "confirmed": "Confirmed",
    "pending": "Pending",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
    "checked in": "Checked In",
    "checked out": "Checked Out",
}

CHANNEL_NAMES = {
    "booking.com": "Booking.com",
    "airbnb": "Airbnb",
    "direct": "Direct",
    "property finder": "Property Finder",
    "bayut": "Bayut",
    "dubizzle": "Dubizzle",
}

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    property_code TEXT NOT NULL,
    guest_name TEXT NOT NULL,
    phone TEXT,
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL,
    amount_aed_minor INTEGER NOT NULL,
    status TEXT NOT NULL,
    responsible_manager TEXT NOT NULL,
    channel TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT,
    payload_hash TEXT NOT NULL
);

DROP TABLE IF EXISTS sync_errors;
CREATE TABLE sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_number INTEGER,
    record_id TEXT,
    error TEXT NOT NULL
);
"""


def clean_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFC", value or "")
    return " ".join(normalized.strip().split())


def normalize_record_id(value: str | None) -> str:
    return clean_text(value).upper()


def google_csv_url(sheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet_url)
    if not match:
        raise ValueError("Google Sheet ID was not found in the URL")

    parsed = urlparse(sheet_url)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    gid = (query.get("gid") or fragment.get("gid") or ["0"])[0]
    sheet_id = match.group(1)
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/"
        f"export?format=csv&gid={gid}"
    )


def download_sheet(sheet_url: str) -> str:
    request = Request(
        google_csv_url(sheet_url),
        headers={"User-Agent": "booking-sync/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def read_csv_file(file_path: str) -> str:
    return Path(file_path).read_text(encoding="utf-8-sig")


def parse_csv(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("CSV header is missing")

    columns = {clean_text(name) for name in reader.fieldnames if name}
    missing = sorted(EXPECTED_COLUMNS - columns)
    if missing:
        raise ValueError(f"Required columns are missing: {', '.join(missing)}")

    return [
        {clean_text(key): value or "" for key, value in row.items() if key is not None}
        for row in reader
    ]


def parse_date(value: str) -> datetime:
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(clean_text(value), date_format)
        except ValueError:
            pass
    raise ValueError(f"invalid date: {value!r}")


def parse_updated_at(value: str) -> datetime:
    for date_format in DATETIME_FORMATS:
        try:
            return datetime.strptime(clean_text(value), date_format)
        except ValueError:
            pass
    raise ValueError(f"invalid updated_at: {value!r}")


def normalize_phone(value: str) -> str | None:
    value = clean_text(value)
    if not value:
        return None

    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = "971" + digits[1:]

    if not 8 <= len(digits) <= 15:
        raise ValueError(f"invalid phone: {value!r}")
    return "+" + digits


def raw_row_hash(row: dict[str, str]) -> str:
    values = {key: clean_text(value) for key, value in row.items()}
    values["record_id"] = normalize_record_id(row.get("record_id"))
    text = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def booking_hash(booking: dict[str, object]) -> str:
    values = {key: value for key, value in booking.items() if key != "payload_hash"}
    text = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_amount_minor(value: str) -> int:
    try:
        amount = Decimal(clean_text(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {value!r}") from exc

    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError("amount must have no more than two decimal places")
    if minor <= 0:
        raise ValueError("amount must be greater than zero")
    return int(minor)


def prepare_row(
    row: dict[str, str],
    record_id: str,
    updated_at: datetime,
) -> dict[str, object]:
    required = (
        "property_code",
        "guest_name",
        "check_in",
        "check_out",
        "amount_aed",
        "status",
        "responsible_manager",
        "channel",
    )
    errors = [f"{name} is required" for name in required if not clean_text(row.get(name))]
    if errors:
        raise ValueError("; ".join(errors))

    check_in = parse_date(row["check_in"])
    check_out = parse_date(row["check_out"])
    if check_out <= check_in:
        raise ValueError("check_out must be later than check_in")

    status_key = clean_text(row["status"]).casefold().replace("_", " ")
    if status_key not in STATUS_NAMES:
        raise ValueError(f"unknown status: {row['status']!r}")

    channel = clean_text(row["channel"])
    channel = CHANNEL_NAMES.get(channel.casefold(), channel)

    booking: dict[str, object] = {
        "record_id": record_id,
        "property_code": clean_text(row["property_code"]),
        "guest_name": clean_text(row["guest_name"]),
        "phone": normalize_phone(row.get("phone", "")),
        "check_in": check_in.strftime("%Y-%m-%d"),
        "check_out": check_out.strftime("%Y-%m-%d"),
        "amount_aed_minor": parse_amount_minor(row["amount_aed"]),
        "status": STATUS_NAMES[status_key],
        "responsible_manager": clean_text(row["responsible_manager"]),
        "channel": channel,
        "updated_at": updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "notes": clean_text(row.get("notes")) or None,
    }
    booking["payload_hash"] = booking_hash(booking)
    return booking


def save_error(
    connection: sqlite3.Connection,
    row_number: int,
    record_id: str | None,
    error: str,
) -> None:
    connection.execute(
        """
        INSERT INTO sync_errors(row_number, record_id, error)
        VALUES (?, ?, ?)
        """,
        (row_number, record_id, error),
    )


def upsert_booking(connection: sqlite3.Connection, booking: dict[str, object]) -> str:
    current = connection.execute(
        "SELECT updated_at, payload_hash FROM bookings WHERE record_id = ?",
        (booking["record_id"],),
    ).fetchone()

    if current is None:
        columns = ", ".join(booking)
        placeholders = ", ".join("?" for _ in booking)
        connection.execute(
            f"INSERT INTO bookings ({columns}) VALUES ({placeholders})",
            tuple(booking.values()),
        )
        return "inserted"

    if booking["updated_at"] < current[0]:
        return "stale"
    if booking["updated_at"] == current[0]:
        if booking["payload_hash"] == current[1]:
            return "unchanged"
        return "conflict"

    columns_to_update = [name for name in booking if name != "record_id"]
    set_sql = ", ".join(f"{name} = ?" for name in columns_to_update)
    values = [booking[name] for name in columns_to_update]
    values.append(booking["record_id"])
    connection.execute(
        f"UPDATE bookings SET {set_sql} WHERE record_id = ?",
        values,
    )
    return "updated"


def sync_rows(rows: list[dict[str, str]], database_path: str) -> dict[str, int]:
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "rows_read": len(rows),
        "blank_rows": 0,
        "valid_records": 0,
        "invalid_rows": 0,
        "duplicate_rows": 0,
        "conflicts": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "stale": 0,
    }

    connection = sqlite3.connect(database)
    try:
        connection.executescript(CREATE_TABLES_SQL)

        # First group rows by ID and updated_at. Full validation happens only
        # after the newest version is selected.
        versions: dict[str, list[tuple[int, dict[str, str], datetime]]] = {}
        for row_number, row in enumerate(rows, start=2):
            if not any(clean_text(value) for value in row.values()):
                summary["blank_rows"] += 1
                continue

            record_id = normalize_record_id(row.get("record_id"))
            if not record_id:
                summary["invalid_rows"] += 1
                save_error(connection, row_number, None, "record_id is required")
                continue
            try:
                updated_at = parse_updated_at(row.get("updated_at", ""))
            except ValueError as exc:
                summary["invalid_rows"] += 1
                save_error(connection, row_number, record_id, str(exc))
                continue

            normalized_row = dict(row)
            normalized_row["record_id"] = record_id
            versions.setdefault(record_id, []).append(
                (row_number, normalized_row, updated_at)
            )

        prepared: list[tuple[int, dict[str, object]]] = []
        for record_id, record_versions in versions.items():
            summary["duplicate_rows"] += len(record_versions) - 1
            newest_time = max(item[2] for item in record_versions)
            newest = [item for item in record_versions if item[2] == newest_time]

            if len({raw_row_hash(item[1]) for item in newest}) > 1:
                summary["conflicts"] += 1
                save_error(
                    connection,
                    newest[0][0],
                    record_id,
                    "VersionConflict: same updated_at with different data",
                )
                continue

            row_number, row, updated_at = newest[0]
            try:
                booking = prepare_row(row, record_id, updated_at)
            except ValueError as exc:
                summary["invalid_rows"] += 1
                save_error(connection, row_number, record_id, str(exc))
                continue
            prepared.append((row_number, booking))

        summary["valid_records"] = len(prepared)
        for row_number, booking in sorted(prepared, key=lambda item: item[1]["record_id"]):
            result = upsert_booking(connection, booking)
            if result == "conflict":
                summary["conflicts"] += 1
                save_error(
                    connection,
                    row_number,
                    str(booking["record_id"]),
                    "VersionConflict: database has different data for this updated_at",
                )
            else:
                summary[result] += 1

        connection.commit()
        return summary
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
