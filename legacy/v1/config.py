"""
GitHub Radar — shared config. SINGLE SOURCE OF TRUTH.

Every script imports the spreadsheet target from here. Nothing hardcodes a
sheet URL or ID of its own. This is the fix for the bug where setup_sheet.py
and smoke_test.py pointed at different sheets and created tabs in the wrong
one: there is now exactly one place to change, so they cannot drift apart.

To switch sheets, set RADAR_SHEET_ID in the environment — nowhere else.
No sheet ID is committed to the repository.
"""

import os
from pathlib import Path

# ─── The ONE place the target spreadsheet is defined ──────────────────────────
# Set RADAR_SHEET_ID in your shell before running any V1 script.
SHEET_ID = os.environ.get("RADAR_SHEET_ID", "")
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"

# ─── Shared credential locations ──────────────────────────────────────────────
# GOOGLE_SERVICE_ACCOUNT_FILE overrides where the service-account key is read.
SERVICE_ACCOUNT_FILE = Path(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account-key.json"))
ENV_FILE = Path(".env")

# ─── Tab names ────────────────────────────────────────────────────────────────
SOURCE_TAB = "radar_source"
SIGNALS_TAB = "radar_signals"
HISTORY_TAB = "radar_history"

# ─── Tab schemas (SINGLE SOURCE OF TRUTH) ─────────────────────────────────────
# Both setup_sheet.py (creates the tabs) and radar_cycle.py (writes rows)
# import these. Defining the column order in exactly one place means the
# writer can never drift out of sync with the actual sheet headers.
SOURCE_HEADERS = [
    "repo", "ecosystem",
    "stars", "forks", "pushed_at", "default_branch",
]

# Signals: the live upsert queue. status/first_seen are PRESERVED across
# upserts; last_seen/last_cycle_id are refreshed each cycle.
# NOTE: branch_leading/branch_lagging were APPENDED AT THE END (not grouped
# with the other evidence columns) on purpose — the tab already had data, and
# inserting mid-schema would have shifted every later column and misaligned
# existing rows (and the first_seen/status preservation read). Appending keeps
# existing rows aligned; new cells read blank until the next --write backfill.
SIGNALS_HEADERS = [
    "repo", "repo_url", "ecosystem",
    "leading_score", "lagging_score",
    "leading_hits", "lagging_hits", "ecosystem_markers",
    "velocity", "author_diversity", "category_breadth", "has_contract_files",
    "commits_30d", "commits_scanned", "branches_scanned",
    "pushed_at", "default_branch", "stars", "forks", "created_at",
    "status", "first_seen", "last_seen", "last_cycle_id",
    "branch_leading", "branch_lagging",   # appended at end (see note above)
]

# History: append-only event log. Same as signals MINUS preserved-state
# columns (status, first_seen, last_seen, last_cycle_id), PLUS per-event
# stamps (cycle_id, detected_at).
HISTORY_HEADERS = [
    "repo", "repo_url", "ecosystem",
    "leading_score", "lagging_score",
    "leading_hits", "lagging_hits", "ecosystem_markers",
    "velocity", "author_diversity", "category_breadth", "has_contract_files",
    "commits_30d", "commits_scanned", "branches_scanned",
    "pushed_at", "default_branch", "stars", "forks", "created_at",
    "cycle_id", "detected_at",
    "branch_leading", "branch_lagging",   # appended at end (see note above)
]


def load_env_token():
    """Read GITHUB_TOKEN from the .env file by hand (no python-dotenv dep).

    Format expected: GITHUB_TOKEN=ghp_xxx  (no quotes, no spaces around =)
    """
    if not ENV_FILE.exists():
        raise FileNotFoundError(
            ".env file not found. Create it in the project folder with a line:\n"
            "    GITHUB_TOKEN=your_token_here"
        )
    token = ""
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "GITHUB_TOKEN":
            token = value.strip()
    if not token or token == "paste_your_fresh_token_here":
        raise ValueError(
            "GITHUB_TOKEN is missing or still the placeholder in .env. "
            "Paste your fresh token there and save."
        )
    return token
