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
  3) For ambiguous month typos (e.g. '15-Ma-2026' -> Mar or May) a second
     pass inspects neighbouring rows in the same column. If every supporting
     neighbour agrees on exactly one of the candidate months (with a
     minimum support threshold) the cell is auto-corrected; otherwise it is
     flagged for manual review.
  4) Anything that remains ambiguous is flagged (never guessed) and the
     workflow fails so the owner is notified.

Accuracy rules (strictly enforced):
  - Date input must contain a full day + month + year signal. '2026' or
    'Apr 2026' alone are rejected — we do not fabricate missing components.
  - Year must fall in [1900, 2100]; day/month must form a real calendar date.
  - For ambiguous month prefixes, candidate months are narrowed by day
    validity first (e.g. '31-Ju-2026' resolves to Jul — Jun has 30 days).
  - Neighbour inference only fires when exactly one candidate has at least
    NEIGHBOUR_MIN_SUPPORT neighbours agreeing AND no other candidate has
    any neighbour support.
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
from datetime import datetime, timedelta
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

# Neighbour-inference tuning.
NEIGHBOUR_WINDOW = 10       # rows to look at on either side
NEIGHBOUR_MIN_SUPPORT = 3   # minimum agreeing neighbours to commit a fix

# Sheets stores dates as days since 1899-12-30 (Excel-compatible epoch).
SHEETS_EPOCH = datetime(1899, 12, 30)


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
    short_prefix = [s for s in MONTHS_SHORT if s.lower().startswith(t)]
    if len(short_prefix) == 1:
        return short_prefix[0]
    long_prefix = [short for long_name, short in zip(MONTHS_LONG, MONTHS_SHORT)
                   if long_name.lower().startswith(t)]
    if len(long_prefix) == 1:
        return long_prefix[0]
    return None


def _candidate_months(token: str) -> list[str]:
    """All short-month names that could match the token by exact or prefix."""
    t = token.strip().lower()
    if not t:
        return []
    for s in MONTHS_SHORT:
        if t == s.lower():
            return [s]
    for long_name, short in zip(MONTHS_LONG, MONTHS_SHORT):
        if t == long_name.lower():
            return [short]
    short_prefix = [s for s in MONTHS_SHORT if s.lower().startswith(t)]
    if short_prefix:
        return short_prefix
    long_prefix = [short for long_name, short in zip(MONTHS_LONG, MONTHS_SHORT)
                   if long_name.lower().startswith(t)]
    return long_prefix


_ORDINAL = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_STRUCTURED = re.compile(r"^(\d{1,2})[-\s/.]+([A-Za-z]+)[-\s/.]+(\d{2,4})$")


def analyze_date(text: str) -> tuple[Optional[str], list[str], Optional[int], Optional[int]]:
    """Rich date analysis.

    Returns (canonical, candidates, day, year):
      - canonical: 'D-MMM-YYYY' if fully resolved, else None.
      - candidates: list of short month names that are still possible when the
        input is only ambiguous on the month token. Empty when canonical is
        set or when the input is unparseable.
      - day, year: set whenever day and year were extracted successfully,
        even for ambiguous cases (so neighbour inference can reuse them).
    """
    if not isinstance(text, str):
        return None, [], None, None
    t = text.strip()
    if not t:
        return None, [], None, None
    t = _ORDINAL.sub(r"\1", t)

    # Strategy A: D sep WordMonth sep Y.
    m = _STRUCTURED.match(t)
    if m:
        day_s, mon_s, yr_s = m.groups()
        day, year = int(day_s), int(yr_s)
        if year < 100:
            year += 2000
        if not (1900 <= year <= 2100):
            return None, [], None, None

        cands = _candidate_months(mon_s)
        if not cands:
            return None, [], None, None

        # Narrow by day validity (e.g. day 31 rules out Jun for 'Ju').
        valid = []
        for c in cands:
            try:
                datetime(year, MONTHS_SHORT.index(c) + 1, day)
                valid.append(c)
            except ValueError:
                continue
        if not valid:
            return None, [], None, None
        if len(valid) == 1:
            return f"{day}-{valid[0]}-{year}", [], day, year
        return None, valid, day, year

    # Strategy B: numeric orderings (DD/MM/YYYY, YYYY-MM-DD, etc.)
    digit_groups = re.findall(r"\d+", t)
    has_month_word = bool(re.search(r"[A-Za-z]{3,}", t))
    if not (len(digit_groups) >= 3
            or (len(digit_groups) >= 2 and has_month_word)):
        return None, [], None, None
    try:
        dt = dtparser.parse(t, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None, [], None, None
    if not (1900 <= dt.year <= 2100):
        return None, [], None, None
    return (f"{dt.day}-{MONTHS_SHORT[dt.month - 1]}-{dt.year}",
            [], dt.day, dt.year)


def parse_date(text: str) -> Optional[str]:
    """Thin wrapper: return canonical date string only."""
    canonical, _, _, _ = analyze_date(text)
    return canonical


def infer_month(
    candidates: list[str],
    neighbour_months: list[int],
    min_support: int = NEIGHBOUR_MIN_SUPPORT,
) -> Optional[str]:
    """Pick a single candidate month from neighbour context, deterministically.

    Rule: exactly one candidate must have >= min_support neighbours in that
    month, and no other candidate may have any neighbour support. Neighbours
    in non-candidate months are ignored (they neither support nor oppose).
    """
    if not candidates:
        return None
    hits = {c: neighbour_months.count(MONTHS_SHORT.index(c) + 1)
            for c in candidates}
    supported = [c for c, n in hits.items() if n > 0]
    if len(supported) == 1 and hits[supported[0]] >= min_support:
        return supported[0]
    return None


def serial_to_month(serial: Union[int, float]) -> int:
    """Convert a Sheets date serial to its calendar month (1-12)."""
    return (SHEETS_EPOCH + timedelta(days=int(serial))).month


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
    date_inferred: list[tuple[str, str]] = []  # fixes from neighbour inference
    date_anomalies: list[str] = []
    amount_writes: list[tuple[str, Union[int, float]]] = []
    amount_clears: list[str] = []
    amount_anomalies: list[str] = []

    # Phase 1a: scan date columns. Record both clean dates (as neighbour
    # context) and ambiguous cells for phase 1b resolution.
    row_month: dict[tuple[str, str], dict[int, int]] = {}
    ambiguous_dates: list[dict] = []

    for tab, col, start, end in DATE_COLS:
        key = (tab, col)
        row_month[key] = {}
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
            if isinstance(v, (int, float)):
                row_month[key][i] = serial_to_month(v)
                continue
            if not (isinstance(v, str) and v.strip()):
                continue
            canonical, candidates, day, year = analyze_date(v)
            if canonical:
                date_fixes.append((f"{tab}!{col}{i}", canonical))
                month_num = MONTHS_SHORT.index(canonical.split("-")[1]) + 1
                row_month[key][i] = month_num
            elif candidates and day is not None and year is not None:
                ambiguous_dates.append({
                    "ref": f"{tab}!{col}{i}",
                    "key": key, "row": i,
                    "day": day, "year": year,
                    "candidates": candidates,
                })
            else:
                date_anomalies.append(f"{tab}!{col}{i}")

    # Phase 1b: neighbour-based inference for ambiguous cells.
    for amb in ambiguous_dates:
        neighbours = [
            month for r, month in row_month[amb["key"]].items()
            if r != amb["row"]
            and abs(r - amb["row"]) <= NEIGHBOUR_WINDOW
        ]
        inferred = infer_month(amb["candidates"], neighbours)
        if inferred is not None:
            canonical = f"{amb['day']}-{inferred}-{amb['year']}"
            date_inferred.append((amb["ref"], canonical))
            month_num = MONTHS_SHORT.index(inferred) + 1
            row_month[amb["key"]][amb["row"]] = month_num
        else:
            date_anomalies.append(amb["ref"])

    # Phase 2: scan amount columns.
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

    # Apply writes.
    data_updates = (
        [{"range": r, "values": [[v]]} for r, v in date_fixes]
        + [{"range": r, "values": [[v]]} for r, v in date_inferred]
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

    # Verify dashboard.
    dash = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="Dashboard!A1:Z200",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])
    dash_errors = sum(
        1 for row in dash for cell in row
        if isinstance(cell, str) and cell.startswith("#VALUE!")
    )

    # Reporting — counts only, refs only for anomalies.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        "## Sheet audit",
        "",
        "| metric | count |",
        "| --- | ---: |",
        f"| Date cells auto-corrected (unambiguous) | {len(date_fixes)} |",
        f"| Date cells inferred from neighbours | {len(date_inferred)} |",
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
        f"inferred_dates={len(date_inferred)} "
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
