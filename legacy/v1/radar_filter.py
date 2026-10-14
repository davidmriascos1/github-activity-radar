"""
GitHub Radar — radar_filter.py  (Solidity source feed for radar_source)

Job: produce the list of repos worth scanning, and write it to the
`radar_source` tab. This is the SOURCE feed for radar_cycle.py.

Design — two stages, "permissive at source, strict at signal":

  STAGE A (DuckDB, ZERO GitHub calls, LOOSE):
    Roster of candidate repos from the Electric Capital DuckDB. Filters
    that are free to apply here: EVM/Solidity ecosystem membership, crypto,
    not blacklisted, not a competitor, and a LOW junk-floor on stars just
    to skip toy/homework repos before we spend API calls. Stars are a
    reputation proxy, NOT an activity proxy, so the floor is deliberately
    low and there is no fork floor.

  STAGE B (GitHub API, ~2 calls/repo, STRICT — the REAL filter):
    Value = ACTIVITY, not popularity. Keep a repo only if it shows recent
    commit activity. The OR-pushed_at half is a false-negative guard for
    branch-heavy teams whose default-branch commit count looks quiet.

Eligibility (does it actually contain .sol files?) is NOT checked here —
radar_cycle.py already records that per repo as `has_contract_files`.
Duplicating it would double the git-trees API calls for the same answer.

Writing: MERGE into radar_source (append new repos, skip ones already
there) so hand-added seed repos are preserved. Dry-run by default; pass
--write to actually write.

Numbers (tunable, see the constants below):
    ACTIVITY_WINDOW_DAYS = 60
    MIN_COMMITS_IN_WINDOW = 10
    MIN_STARS (junk floor)= 10   (no fork floor)
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import gspread
import requests
from google.oauth2.service_account import Credentials

from config import (
    SPREADSHEET_URL, SERVICE_ACCOUNT_FILE, load_env_token,
    SOURCE_TAB, SOURCE_HEADERS,
)

# ─── Configuration ────────────────────────────────────────────
DUCKDB_PATH = Path.home() / "Library/Caches/open-dev-data/data.duckdb"

ECOSYSTEM_LABEL = "Solidity"  # the value written to radar_source.ecosystem

# Stage A — require membership in the 'Solidity' ecosystem by EXACT name.
# NOT a LIKE match: there are junk lookalikes (SolidStamp, Solidified, Solidly)
# that a substring match would wrongly pull in. NOT the EVM-chain set either:
# chain membership swept in tooling/infra/non-Solidity repos (go-ethereum,
# foundry, sui, capstone...). Exact 'Solidity' membership is the clean cut.
SOLIDITY_ECOSYSTEM = "Solidity"

COMPETITOR_PATTERNS = [
    "/openzeppelin/openzeppelin",
    "/openzeppelin/ethernaut",
    "cyfrin", "trailofbits", "consensys", "certik", "code-423n4",
]

# ─── The three numbers ────────────────────────────────────────
ACTIVITY_WINDOW_DAYS = 60     # Stage B look-back window (N)
MIN_COMMITS_IN_WINDOW = 10    # Stage B commit threshold (T)
MIN_STARS = 10                # Stage A junk floor (S_min); no fork floor

API_PACING_SECONDS = 0.5


def now_utc():
    return datetime.now(timezone.utc)


# ─── Stage A: DuckDB roster (no API calls, LOOSE) ─────────────
def stage_a_roster():
    if not DUCKDB_PATH.exists():
        raise FileNotFoundError(
            f"DuckDB file not found at {DUCKDB_PATH}. This script only READS it; "
            f"it's built by a separate Electric Capital ingestion step."
        )

    print(f"📂 STAGE A — querying DuckDB at {DUCKDB_PATH}")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)

    competitor_filters = " AND ".join(
        f"LOWER(r.link) NOT LIKE '%{pattern}%'" for pattern in COMPETITOR_PATTERNS
    )

    # Exact 'Solidity' ecosystem membership (bound param = literal equality, no
    # wildcard). is_crypto and repo_created_at are intentionally NOT filtered:
    # repo_created_at is unreliable in this DB (created>=2025 returned 0), and
    # the exact-name Solidity match already scopes us correctly.
    query = f"""
        SELECT DISTINCT
            r.id, r.name, r.num_stars, r.num_forks, r.link
        FROM repos r
        JOIN ecosystems_repos er ON r.id = er.repo_id
        JOIN ecosystems e ON er.ecosystem_id = e.id
        WHERE e.name = ?
          AND r.is_blacklist = 0
          AND r.num_stars >= ?
          AND {competitor_filters}
        ORDER BY r.num_stars DESC
    """
    params = [SOLIDITY_ECOSYSTEM, MIN_STARS]
    rows = con.execute(query, params).fetchall()
    con.close()

    candidates = []
    for _id, name, stars, forks, link in rows:
        slug = (name or "").strip()
        # Fall back to deriving owner/name from the link if `name` isn't a slug.
        if "/" not in slug and link:
            slug = link.rstrip("/").replace("https://github.com/", "").replace("http://github.com/", "")
        if slug:
            candidates.append({
                "repo": slug, "stars": stars, "forks": forks, "repo_url": link,
            })

    print(f"✅ STAGE A returned {len(candidates)} candidate repos "
          f"('Solidity' ecosystem, stars ≥ {MIN_STARS}, no fork floor).")
    return candidates


# ─── GitHub helper with rate-limit + transient-error handling ─
def github_get(url, headers, params=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 403 and "rate limit" in r.text.lower():
                reset = int(r.headers.get("x-ratelimit-reset", 0))
                wait = max(60, reset - int(time.time()) + 5)
                print(f"   ⏸  Rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            return r
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                backoff = 2 ** attempt
                print(f"   ↻  Retry {attempt + 1}/{max_retries} after {backoff}s ({type(e).__name__})")
                time.sleep(backoff)
                continue
            print(f"   ⚠️  Request failed after {max_retries} attempts: {e}")
            return None
    return None


# ─── Stage B: GitHub activity gate (STRICT — the real filter) ─
def stage_b_activity(candidates, headers, limit=None):
    since_str = (now_utc() - timedelta(days=ACTIVITY_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # For --limit testing, sample RANDOMLY rather than taking the star-ranked
    # head — otherwise the preview is just the famous repos again (the exact
    # sampling bias that hid the targeting problem last time).
    if limit and limit < len(candidates):
        pool = random.sample(candidates, limit)
        print(f"\n🎲 --limit {limit}: random sample of candidates (not star-ranked).")
    else:
        pool = candidates

    print(f"\n🌐 STAGE B — activity gate on {len(pool)} candidates "
          f"(keep if commits in {ACTIVITY_WINDOW_DAYS}d ≥ {MIN_COMMITS_IN_WINDOW} "
          f"OR pushed within {ACTIVITY_WINDOW_DAYS}d).")
    print(f"   Rough time: ~{len(pool) * API_PACING_SECONDS * 2 / 60:.0f} min.\n")

    passed = []
    n_archived = n_404 = n_err = n_inactive = 0

    for i, cand in enumerate(pool, 1):
        slug = cand["repo"]
        if i % 50 == 0:
            print(f"   ...checked {i}/{len(pool)}")

        meta = github_get(f"https://api.github.com/repos/{slug}", headers)
        if meta is None:
            n_err += 1
            continue
        if meta.status_code == 404:
            n_404 += 1
            continue
        if not meta.ok:
            n_err += 1
            continue
        data = meta.json()
        if data.get("archived", False):
            n_archived += 1
            continue

        default_branch = data.get("default_branch", "main")
        pushed_at_str = data.get("pushed_at", "") or ""

        # commits in window on the default branch
        commits_r = github_get(
            f"https://api.github.com/repos/{slug}/commits",
            headers,
            params={"since": since_str, "per_page": 100},
        )
        commits_in_window = len(commits_r.json()) if (commits_r and commits_r.ok) else 0

        # OR-fallback: any push within the window (branch-heavy-team guard)
        recent_push = False
        if pushed_at_str:
            try:
                pushed_dt = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
                recent_push = (now_utc() - pushed_dt).days <= ACTIVITY_WINDOW_DAYS
            except (ValueError, TypeError):
                pass

        if commits_in_window < MIN_COMMITS_IN_WINDOW and not recent_push:
            n_inactive += 1
            time.sleep(API_PACING_SECONDS)
            continue

        passed.append({
            "repo": slug,
            "ecosystem": ECOSYSTEM_LABEL,
            "stars": data.get("stargazers_count", cand.get("stars", 0)),
            "forks": data.get("forks_count", cand.get("forks", 0)),
            "pushed_at": pushed_at_str[:10],
            "default_branch": default_branch,
            "_commits_in_window": commits_in_window,  # for the printout only
        })
        time.sleep(API_PACING_SECONDS)

    print(f"\n✅ STAGE B kept {len(passed)} active repos.")
    print(f"   Dropped: {n_inactive} inactive, {n_archived} archived, "
          f"{n_404} 404s, {n_err} errors.")
    return passed


# ─── Google Sheets: read existing + merge-write ───────────────
def open_spreadsheet(write):
    if not SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError("service-account-key.json not found in the project folder.")
    if write:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
    else:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_url(SPREADSHEET_URL)


def merge_into_source(passed, write):
    ss = open_spreadsheet(write=write)
    ws = ss.worksheet(SOURCE_TAB)

    existing = {r.strip() for r in ws.col_values(1)[1:] if r.strip()}  # skip header
    print(f"\nradar_source already holds {len(existing)} repos.")

    new_rows = []
    already = 0
    for r in passed:
        if r["repo"] in existing:
            already += 1
            continue
        # render in canonical header order
        d = {
            "repo": r["repo"], "ecosystem": r["ecosystem"],
            "stars": r["stars"], "forks": r["forks"],
            "pushed_at": r["pushed_at"], "default_branch": r["default_branch"],
        }
        new_rows.append([d.get(h, "") for h in SOURCE_HEADERS])

    print(f"Planned: {len(new_rows)} new repo(s) to append; "
          f"{already} already present (skipped).")

    if not write:
        # Preview sorted by pushed_at (freshness), NOT star-rank, so the sample
        # isn't the famous head. pushed_at is col index 4 in SOURCE_HEADERS.
        preview = sorted(new_rows, key=lambda r: r[4], reverse=True)
        print("\n🟡 DRY-RUN: nothing written. Preview of up to 15 new repos "
              "(freshest by pushed_at):")
        for row in preview[:15]:
            print(f"     + {row[0]}  (stars={row[2]}, pushed={row[4]}, branch={row[5]})")
        if len(new_rows) > 15:
            print(f"     … and {len(new_rows) - 15} more")
        print("\nRe-run with --write to append these to radar_source.\n")
        return

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
        print(f"\n✅ Appended {len(new_rows)} new repo(s) to {SOURCE_TAB}. "
              f"Total now {len(existing) + len(new_rows)}.\n")
    else:
        print("\n🎯 Nothing to append — radar_source already covers all active repos.\n")


def parse_args():
    p = argparse.ArgumentParser(description="GitHub Radar — Solidity source feed.")
    p.add_argument("--write", action="store_true",
                   help="Append new repos to radar_source. Omit for dry-run.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap Stage-B candidates (for testing; saves API calls).")
    return p.parse_args()


def main():
    args = parse_args()
    mode = "WRITE" if args.write else "DRY-RUN (preview only)"
    print(f"\nGitHub Radar — Solidity source feed.   MODE: {mode}")
    print(f"Window={ACTIVITY_WINDOW_DAYS}d  MinCommits={MIN_COMMITS_IN_WINDOW}  "
          f"StarFloor={MIN_STARS}\n")

    candidates = stage_a_roster()

    token = load_env_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    passed = stage_b_activity(candidates, headers, limit=args.limit)

    # Show the freshest of what passed, as a spot-check.
    passed_sorted = sorted(passed, key=lambda r: r.get("_commits_in_window", 0), reverse=True)
    print(f"\n📊 Most active repos that passed (top 10 by commits in window):")
    for r in passed_sorted[:10]:
        print(f"   {r['_commits_in_window']:>4} commits · pushed {r['pushed_at']} — {r['repo']}")

    merge_into_source(passed, write=args.write)


if __name__ == "__main__":
    main()
