"""
Second-layer audit for the Logistikus finance sheet.

What it does:
  1) Scans the three data tabs for text in columns that formulas treat as
     numeric (dates, amounts).
  2) Auto-corrects every entry it can parse accurately:
       - Date cells: typos ('15-Ap-2026'), alternative orderings
         ('2026-04-15', '15/04/2026'), ordinal suffixes ('15th Apr 2026'),
         whitespace, capitalisation variants.
       - Amount cells: currency symbols, commas, surrounding whitespace,
         whitespace-only cells cleared.
  3) Anything genuinely ambiguous is flagged (never guessed) and the
     workflow fails so the owner is notified.

Accuracy rules (strictly enforced):
  - Date input must contain a full day + month + year signal. '2026' or
    'Apr 2026' alone are rejected — we do not fabricate missing components.
  - A month token that matches more than one calendar month (e.g. 'Ma',
    'Ju') is rejected as ambiguous.
  - Year must fall in [1900, 2100]; day/month must form a real calendar date.
  - Amount input must parse as a plain decimal after stripping currency /
    commas / whitespace. Scientific notation and alphabetic residue are
    rejected.

Privacy:
  - Logs contain counts only. Cell values, rider names, customer names,
    descriptions, and the sheet ID are never printed.
  - Credentials are read from env vars (GCP_SA_JSON, SHEET_ID) injected
    from GitHub Secrets at runtime.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from typing import Optional, Union

from dateutil import parser as dtparser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

DATE_COLS = [
    ("Deliveries",    "B", 6, 1005),
    ("Subscriptions", "D", 6, 1005),
    ("Subscriptions", "E", 6, 1005),
    ("Expenses",      "A", 5, 1004),
]
AMOUNT_COLS = [
    ("Deliveries",    "H", 6, 1005),
    ("Subscriptions", "G", 6, 1005),
    ("Expenses",      "E", 5, 1004),
]


def normalize_month(token: str) -> Optional[str]:
    """Canonical 3-letter month, or None if the token is ambiguous."""
    t = token.strip().lower()
    if not t:
        return None
    for s in MONTHS_SHORT:
        if t == s.lower():
            return s
    for long_name, short in zip(MONTHS_LONG, MONTHS_SHORT):
        if t == long_name.lower():
            return short
    # Unique-prefix match only. Rejects ambiguous ('Ma' -> Mar/May) and
    # non-prefix typos ('Arp' -> ???) so we never guess.
    short_prefix = [s for s in MONTHS_SHORT if s.lower().startswith(t)]
    if len(short_prefix) == 1:
        return short_prefix[0]
    long_prefix = [short for long_name, short in zip(MONTHS_LONG, MONTHS_SHORT)
                   if long_name.lower().startswith(t)]
    if len(long_prefix) == 1:
        return long_prefix[0]
    return None


_ORDINAL = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_STRUCTURED = re.compile(r"^(\d{1,2})[-\s/.]+([A-Za-z]+)[-\s/.]+(\d{2,4})$")


def parse_date(text: str) -> Optional[str]:
    """Return canonical 'D-MMM-YYYY', or None if ambiguous / not a full date."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    t = _ORDINAL.sub(r"\1", t)

    # Strategy A: D sep WordMonth sep Y (day first, month as word)
    m = _STRUCTURED.match(t)
    if m:
        day_s, mon_s, yr_s = m.groups()
        mon = normalize_month(mon_s)
        if mon is None:
            return None  # unknown / ambiguous month word — do not guess
        day, year = int(day_s), int(yr_s)
        if year < 100:
            year += 2000
        if not (1900 <= year <= 2100):
            return None
        try:
            datetime(year, MONTHS_SHORT.index(mon) + 1, day)
        except ValueError:
            return None
        return f"{day}-{mon}-{year}"

    # Strategy B: numeric orderings (DD/MM/YYYY, YYYY-MM-DD, etc.)
    # Guard: require enough components to represent a full date.
    digit_groups = re.findall(r"\d+", t)
    has_month_word = bool(re.search(r"[A-Za-z]{3,}", t))
    # Need day + month + year => 3 numerics, OR 2 numerics + a month word.
    if not (len(digit_groups) >= 3
            or (len(digit_groups) >= 2 and has_month_word)):
        return None
    try:
        dt = dtparser.parse(t, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if not (1900 <= dt.year <= 2100):
        return None
    return f"{dt.day}-{MONTHS_SHORT[dt.month - 1]}-{dt.year}"


_AMOUNT_STRIP = re.compile(r"[₦$€£¥,\s]")
_AMOUNT_VALID = re.compile(r"^-?\d+(\.\d+)?$")


def parse_amount(text: str) -> Optional[Union[str, int, float]]:
    """Parse an amount cell.

    Returns:
        ''              -> clear the cell (was whitespace-only / strippable to nothing)
        int / float     -> numeric value to write back
        None            -> ambiguous, do not touch
    """
    if not isinstance(text, str):
        return None
    if text.strip() == "":
        return ""
    stripped = _AMOUNT_STRIP.sub("", text)
    if stripped == "":
        return ""
    if not _AMOUNT_VALID.match(stripped):
        return None
    n = float(stripped)
    return int(n) if n.is_integer() else n


def build_service():
    sa_json = os.environ.get("GCP_SA_JSON")
    sheet_id = os.environ.get("SHEET_ID")
    if not sa_json or not sheet_id:
        print("::error::Missing GCP_SA_JSON or SHEET_ID secret.")
        sys.exit(2)
    creds = Credentials.from_service_account_info(json.loads(sa_json),
                                                  scopes=SCOPES)
    return build("sheets", "v4", credentials=creds), sheet_id


def audit() -> None:
    service, sheet_id = build_service()

    date_fixes: list[tuple[str, str]] = []
    date_anomalies: list[str] = []
    amount_writes: list[tuple[str, Union[int, float]]] = []
    amount_clears: list[str] = []
    amount_anomalies: list[str] = []

    for tab, col, start, end in DATE_COLS:
        rng = f"{tab}!{col}{start}:{col}{end}"
        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=rng,
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER",
        ).execute().get("values", [])
        for i, row in enumerate(rows, start):
            if not row:
                continue
            v = row[0]
            if isinstance(v, str) and v.strip():
                fixed = parse_date(v)
                if fixed:
                    date_fixes.append((f"{tab}!{col}{i}", fixed))
                else:
                    date_anomalies.append(f"{tab}!{col}{i}")

    for tab, col, start, end in AMOUNT_COLS:
        rng = f"{tab}!{col}{start}:{col}{end}"
        rows = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=rng,
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [])
        for i, row in enumerate(rows, start):
            if not row:
                continue
            v = row[0]
            if not isinstance(v, str):
                continue
            parsed = parse_amount(v)
            if parsed == "":
                amount_clears.append(f"{tab}!{col}{i}")
            elif parsed is None:
                amount_anomalies.append(f"{tab}!{col}{i}")
            else:
                amount_writes.append((f"{tab}!{col}{i}", parsed))

    data_updates = (
        [{"range": r, "values": [[v]]} for r, v in date_fixes]
        + [{"range": r, "values": [[v]]} for r, v in amount_writes]
    )
    if data_updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data_updates},
        ).execute()
    if amount_clears:
        service.spreadsheets().values().batchClear(
            spreadsheetId=sheet_id, body={"ranges": amount_clears},
        ).execute()

    dash = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Dashboard!A1:Z200",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])
    dash_errors = sum(
        1 for row in dash for cell in row
        if isinstance(cell, str) and cell.startswith("#VALUE!")
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        "## Sheet audit",
        "",
        "| metric | count |",
        "| --- | ---: |",
        f"| Date cells auto-corrected | {len(date_fixes)} |",
        f"| Amount cells normalized | {len(amount_writes)} |",
        f"| Whitespace-only cells cleared | {len(amount_clears)} |",
        f"| Date anomalies (manual review) | {len(date_anomalies)} |",
        f"| Amount anomalies (manual review) | {len(amount_anomalies)} |",
        f"| Dashboard #VALUE! cells remaining | {dash_errors} |",
    ]
    if date_anomalies or amount_anomalies:
        lines += ["", "### Cells needing manual review", ""]
        for ref in date_anomalies:
            lines.append(f"- `{ref}` (date column — unparseable / ambiguous)")
        for ref in amount_anomalies:
            lines.append(f"- `{ref}` (amount column — non-numeric text)")
    body = "\n".join(lines) + "\n"
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(body)

    print(
        f"fixed_dates={len(date_fixes)} "
        f"normalized_amounts={len(amount_writes)} "
        f"cleared_whitespace={len(amount_clears)} "
        f"date_anomalies={len(date_anomalies)} "
        f"amount_anomalies={len(amount_anomalies)} "
        f"dashboard_errors={dash_errors}"
    )

    if date_anomalies or amount_anomalies or dash_errors:
        print("::warning::Audit found anomalies requiring manual review.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        audit()
    except HttpError as e:
        print(f"::error::Sheets API error: status={e.resp.status}")
        sys.exit(3)
