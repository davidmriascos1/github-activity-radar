"""
GitHub Radar — Phase 1 setup: create the three storage tabs (headers only).

Creates, in the configured spreadsheet:
  - radar_source   : the input list of repos to scan, one per ecosystem
  - radar_signals  : the upsert queue — one row per active repo (live BD state)
  - radar_history  : append-only log — one row per detection event

SAFETY: this script is idempotent and ENSURES HEADERS without losing data.
For each expected tab:
  - Missing tab            → create it and write the header row.
  - Exists, header correct → leave it completely untouched.
  - Exists, header wrong/stale (row 1 looks like a header but doesn't match)
                           → overwrite row 1 with the correct headers in place.
  - Exists, row 1 is DATA (no header at all — the bug that bit radar_history)
                           → INSERT a header row above it, pushing data down.
                             No data row is ever overwritten or deleted.
You can run it repeatedly with no risk to data already in the sheet.

NOTE ON SCORES (for when we build scoring later): leading_score and
lagging_score are sums of POOL-RELATIVE lift. They are only comparable
WITHIN one ecosystem. Never rank or threshold across ecosystems.
"""

import gspread
from google.oauth2.service_account import Credentials

# Single source of truth for the sheet target AND schemas — see config.py.
from config import (
    SPREADSHEET_URL, SERVICE_ACCOUNT_FILE,
    SOURCE_TAB, SIGNALS_TAB, HISTORY_TAB,
    SOURCE_HEADERS, SIGNALS_HEADERS, HISTORY_HEADERS,
)

TABS = {
    SOURCE_TAB: SOURCE_HEADERS,
    SIGNALS_TAB: SIGNALS_HEADERS,
    HISTORY_TAB: HISTORY_HEADERS,
}


def open_spreadsheet():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError(
            "service-account-key.json not found in the project folder."
        )
    # Write scope: we are creating tabs, so read-only won't do.
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    ss = client.open_by_url(SPREADSHEET_URL)
    print(f"✅ Opened spreadsheet '{ss.title}'.")
    return ss


def _row1_padded(ws, width):
    """Row 1's first `width` cells, padded with '' and stripped, for comparison."""
    row1 = ws.row_values(1)  # trailing empties are trimmed by the API
    return [(row1[i] if i < len(row1) else "").strip() for i in range(width)]


def _ensure_width(ws, width):
    """Make sure the grid has at least `width` columns (so headers fit)."""
    if ws.col_count < width:
        ws.add_cols(width - ws.col_count)


def ensure_tab(ss, title, headers):
    """Create the tab if missing; otherwise ensure row 1 holds the headers,
    never clobbering existing data rows."""
    existing = {ws.title for ws in ss.worksheets()}
    expected = [h.strip() for h in headers]

    # ── Case 1: tab missing → create with headers ──
    if title not in existing:
        ws = ss.add_worksheet(title=title, rows=100, cols=len(headers))
        ws.update(values=[headers], range_name="A1", value_input_option="RAW")
        print(f"✅ Created '{title}' with {len(headers)} headers.")
        return

    ws = ss.worksheet(title)
    actual = _row1_padded(ws, len(headers))

    # ── Case 2: header already correct → do nothing ──
    if actual == expected:
        print(f"•  '{title}' header already correct — left untouched.")
        return

    row1_all_blank = all(cell == "" for cell in actual)
    first_cell = actual[0]

    # ── Case 3: row 1 empty → write headers in place (no data at row 1) ──
    if row1_all_blank:
        _ensure_width(ws, len(headers))
        ws.update(values=[headers], range_name="A1", value_input_option="RAW")
        print(f"🔧 '{title}' row 1 was empty — wrote header row.")
        return

    # ── Case 4: row 1 looks like a (stale/partial) header → overwrite in place.
    # A real data row's first cell is an 'owner/name' slug, never literally the
    # header name 'repo', so matching headers[0] reliably means "this is a
    # header row" and is safe to overwrite without losing a data row.
    if first_cell == expected[0]:
        _ensure_width(ws, len(headers))
        ws.update(values=[headers], range_name="A1", value_input_option="RAW")
        print(f"🔧 '{title}' had a stale/mismatched header — rewrote row 1 headers in place.")
        return

    # ── Case 5: row 1 is DATA (no header) → insert header ABOVE it, no loss ──
    _ensure_width(ws, len(headers))
    ws.insert_row(headers, index=1, value_input_option="RAW")
    print(f"🔧 '{title}' had DATA in row 1 and no header — inserted header row above it "
          f"(existing data preserved, shifted down by 1).")


def main():
    print("\nGitHub Radar — tab setup (idempotent; ensures headers, preserves data).\n")
    ss = open_spreadsheet()
    for title, headers in TABS.items():
        ensure_tab(ss, title, headers)

    # Confirm final state: re-read each tab's row 1 and verify it matches.
    print("\nVerification (re-reading row 1 of each tab):")
    all_ok = True
    for title, headers in TABS.items():
        ws = ss.worksheet(title)
        actual = _row1_padded(ws, len(headers))
        ok = actual == [h.strip() for h in headers]
        all_ok = all_ok and ok
        print(f"   {'✅' if ok else '⚠️ '} {title}: header {'correct' if ok else 'MISMATCH'}")

    if all_ok:
        print("\n🎯 All three radar tabs exist with correct headers.\n")
    else:
        print("\n⚠️  One or more headers still don't match — re-run or inspect.\n")


if __name__ == "__main__":
    main()
