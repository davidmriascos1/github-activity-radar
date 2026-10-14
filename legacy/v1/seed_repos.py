"""
GitHub Radar — seed the radar_source tab with test repos.

Appends a short list of example Solidity repos to radar_source so the
rest of the pipeline has something to read. NON-DESTRUCTIVE and idempotent:
it reads what's already there and only appends repos that are missing, so
re-running won't create duplicates.

To change the test set, edit SEED_REPOS below — that's the only place.
"""

import gspread
from google.oauth2.service_account import Credentials

from config import SPREADSHEET_URL, SERVICE_ACCOUNT_FILE

SOURCE_TAB = "radar_source"
ECOSYSTEM = "Solidity"  # the LANGUAGE, never the VM

# ─── The test set (owner/name). Swap any line freely. ─────────────────────────
# Placeholders: well-known open-source libraries and tooling, chosen so the
# list is obviously an example. Replace with your own test set.
SEED_REPOS = [
    "dapphub/ds-test",
    "ethereum/solidity-examples",
    "PaulRBerg/prb-math",
    "NomicFoundation/hardhat",
]


def open_source_tab():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError("service-account-key.json not found in the project folder.")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    ss = client.open_by_url(SPREADSHEET_URL)
    ws = ss.worksheet(SOURCE_TAB)
    print(f"✅ Opened '{SOURCE_TAB}' in '{ss.title}'.")
    return ws


def main():
    print("\nGitHub Radar — seeding radar_source (append-only, no duplicates).\n")
    ws = open_source_tab()

    # Existing repos (column A), skipping the header row, to avoid duplicates.
    existing_col = ws.col_values(1)[1:]  # drop header
    existing = {r.strip() for r in existing_col if r.strip()}

    to_add = [r for r in SEED_REPOS if r not in existing]
    skipped = [r for r in SEED_REPOS if r in existing]

    if skipped:
        print(f"•  Already present, skipping {len(skipped)}: {', '.join(skipped)}")

    if not to_add:
        print("\n🎯 Nothing to add — radar_source already has all seed repos.\n")
        return

    # Append rows: [repo, ecosystem]. The optional context columns
    # (stars, forks, pushed_at, default_branch) are left blank — the cycle
    # fills those later. We don't write them here.
    rows = [[repo, ECOSYSTEM] for repo in to_add]
    ws.append_rows(rows, value_input_option="RAW")

    print(f"\n✅ Appended {len(to_add)} repos to {SOURCE_TAB}:")
    for repo in to_add:
        print(f"     + {repo}")
    print(f"\n🎯 radar_source now has {len(existing) + len(to_add)} repos total.\n")


if __name__ == "__main__":
    main()
