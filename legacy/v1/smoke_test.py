"""
GitHub Radar — Phase 1, smallest step: a READ-ONLY smoke test.

Purpose: prove our two credentials work, nothing more.
  Probe A: authenticate to Google Sheets, print the first 5 rows of the
           `solidity_repo_commits` tab.
  Probe B: take the first repo from those rows and make ONE GitHub API
           call for its metadata (stars, pushed_at, default_branch).

This script WRITES NOTHING, anywhere. It only reads. Run it as many
times as you like — it cannot change the sheet or GitHub.

The two probes are kept separate on purpose: if one fails, the error
tells you which credential is the problem, so you fix one thing.
"""

import gspread
import requests
from google.oauth2.service_account import Credentials

# Single source of truth for the sheet target and token loading — see config.py.
from config import SPREADSHEET_URL, SERVICE_ACCOUNT_FILE, load_env_token

TARGET_TAB = "radar_source"


# ─── Probe A: Google Sheets (read-only) ───────────────────────────────────────
def probe_google_sheet():
    print("─" * 60)
    print("PROBE A — Google Sheets")
    print("─" * 60)

    if not SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError(
            "service-account-key.json not found in the project folder."
        )

    # Read-only scope is all we need for a smoke test. (The real cycle will
    # ask for write scope later; we deliberately don't here.)
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    print("✅ Authenticated to Google as the service account.")

    ss = client.open_by_url(SPREADSHEET_URL)
    ws = ss.worksheet(TARGET_TAB)
    print(f"✅ Opened tab '{TARGET_TAB}' in spreadsheet '{ss.title}'.")

    all_values = ws.get_all_values()
    if not all_values:
        print("⚠️  The tab is empty — no rows to show.")
        return None

    header = all_values[0]
    first_5 = all_values[:5]
    print(f"\nFirst {len(first_5)} rows (including header):")
    for i, row in enumerate(first_5):
        print(f"  [{i}] {row}")

    # The first column is `repo` (owner/name slug). Grab the first DATA row.
    if len(all_values) < 2:
        print("⚠️  Header present but no data rows.")
        return None
    first_repo_slug = all_values[1][0].strip()
    print(f"\n→ First repo in the sheet: {first_repo_slug}")
    return first_repo_slug


# ─── Probe B: GitHub (one read-only API call) ─────────────────────────────────
def probe_github(slug, token):
    print()
    print("─" * 60)
    print("PROBE B — GitHub (one metadata call)")
    print("─" * 60)

    if not slug:
        print("⚠️  No repo slug from Probe A, skipping GitHub probe.")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{slug}"
    print(f"GET {url}")
    r = requests.get(url, headers=headers, timeout=15)

    # Friendly, specific messages for the common failure codes.
    if r.status_code == 401:
        raise PermissionError(
            "401 Unauthorized — GitHub rejected the token. It's wrong, expired, "
            "or revoked. Generate a fresh one and update .env."
        )
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError(
            "403 Rate limited — unusual on the very first call. Your token may "
            "not be applying. Check the GITHUB_TOKEN value in .env."
        )
    if r.status_code == 404:
        print(f"⚠️  404 — '{slug}' not found (renamed, deleted, or private). "
              f"That's a data issue, not an auth issue — the token worked.")
        return
    r.raise_for_status()

    data = r.json()
    print("✅ GitHub responded. Metadata for", slug, ":")
    print(f"     stars          : {data.get('stargazers_count')}")
    print(f"     pushed_at      : {data.get('pushed_at')}")
    print(f"     default_branch : {data.get('default_branch')}")

    # Show how much GitHub quota we have — handy context, not an error.
    remaining = r.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        print(f"     (GitHub API calls remaining this hour: {remaining})")


def main():
    print("\nGitHub Radar smoke test — READ-ONLY, writes nothing.\n")
    token = load_env_token()
    print("✅ Loaded GITHUB_TOKEN from .env.\n")
    first_repo_slug = probe_google_sheet()
    probe_github(first_repo_slug, token)
    print("\n🎯 Smoke test complete. If you saw two ✅ blocks, both credentials work.\n")


if __name__ == "__main__":
    main()
