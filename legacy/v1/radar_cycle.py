"""
GitHub Radar — radar_cycle.py  (Phase 1, data-gathering FRONT HALF only)

What this does today, for each repo in radar_source:
  1. list its branches
  2. filter to the relevant branch patterns (cap 5 per repo)
  3. fetch commits from the last 30 days across those branches
  4. pool + de-dup the commits by SHA
  5. PRINT: repo, branches scanned, commits pooled, a few sample messages

DRY-RUN: this writes NOTHING to any sheet. No scoring yet. It only reads
radar_source and the GitHub API, then prints.

Deliberately NOT here yet (coming later):
  - rolling-window slice math (with ~10 repos we just process them all)
  - keyword scoring / leading & lagging
  - any write to radar_signals / radar_history
"""

import time
from datetime import datetime, timedelta, timezone

import argparse

import gspread
import requests
from google.oauth2.service_account import Credentials

from config import (
    SPREADSHEET_URL, SERVICE_ACCOUNT_FILE, load_env_token,
    SOURCE_TAB, SIGNALS_TAB, HISTORY_TAB,
    SIGNALS_HEADERS, HISTORY_HEADERS,
)
from keywords import (
    SOLIDITY_KEYWORDS,
    AUDIT_PREP_PHRASES, AUDIT_LAGGING_PHRASES, BRANCH_LEADING_SIGNALS,
)

# ─── Config ───────────────────────────────────────────────────────────────────
COMMIT_WINDOW_DAYS = 30
MAX_BRANCHES_PER_REPO = 5
SAMPLE_COMMITS_TO_SHOW = 3
API_PACING_SECONDS = 0.3  # polite delay between GitHub calls

# Branch-name patterns we consider "relevant" for activity. The default
# branch is ALWAYS included regardless of name; these catch the dev/release
# branches where branch-heavy teams do their real work. Order = priority
# when we have to cap at MAX_BRANCHES_PER_REPO.
RELEVANT_BRANCH_PATTERNS = [
    "develop", "dev", "release", "staging", "audit",
    "main", "master", "next", "rc", "canary", "deploy",
]


def now_utc():
    return datetime.now(timezone.utc)


# ─── Text normalization ───────────────────────────────────────────────────────
def normalize_branch(name):
    """Turn a branch name into space-separated words for matching.

    Branch names glue words with -, /, _ (e.g. `refactor/audit-prep`,
    `mainnet-deploy`), so we replace those with spaces before substring
    matching — otherwise multi-word keywords like `mainnet deploy` could
    never fire on a branch.
    """
    low = name.lower()
    for sep in ("-", "/", "_", "."):
        low = low.replace(sep, " ")
    return " ".join(low.split())


# ─── Keyword scoring (commit messages + branch names) ─────────────────────────
def score_texts(messages, branch_names, keywords=SOLIDITY_KEYWORDS):
    """Score commit messages AND branch names against the lift taxonomy.

    Decision A: a lift keyword hit in a branch name carries the SAME lift as
    in a commit message; we only TRACK which source fired ('commit', 'branch',
    or 'both'). Still once-per-keyword (presence, not frequency).

    leading_score / lagging_score stay PURE sums of validated lift.

    Returns: (leading_score, lagging_score, leading_hits, lagging_hits, markers)
    where *_hits are (keyword, lift, source) triples.
    """
    msg_blob = "\n".join(m.lower() for m in messages)
    branch_blob = "\n".join(normalize_branch(b) for b in branch_names)

    leading_hits, lagging_hits, markers = [], [], []
    for kw in keywords:
        term = kw["keyword"].lower()
        in_msg = term in msg_blob
        in_branch = term in branch_blob
        if not (in_msg or in_branch):
            continue
        source = "both" if (in_msg and in_branch) else ("branch" if in_branch else "commit")
        if kw["bucket"] == "leading":
            leading_hits.append((kw["keyword"], kw["lift"], source))
        elif kw["bucket"] == "lagging":
            lagging_hits.append((kw["keyword"], kw["lift"], source))
        else:  # descriptive
            markers.append(kw["keyword"])
    leading_score = round(sum(l for _, l, _ in leading_hits), 2)
    lagging_score = round(sum(l for _, l, _ in lagging_hits), 2)
    return leading_score, lagging_score, leading_hits, lagging_hits, markers


def score_branch_signals(branch_names):
    """Branch-name signals with NO validated lift (Decision B).

    Kept OUT of leading_score/lagging_score. Reported separately as evidence.
    Bucketing (Decision C): audit-prep/pre-audit → leading; bare/post audit →
    lagging; mainnet-deploy/release-candidate → leading.

    Returns (branch_leading, branch_lagging), each a list of (label, branch)
    with one entry per distinct label (first matching branch shown).
    """
    leading, lagging = [], []
    seen_lead, seen_lag = set(), set()

    for raw in branch_names:
        nb = normalize_branch(raw)

        # audit: prep wins precedence over bare/post (Decision C)
        if any(p in nb for p in AUDIT_PREP_PHRASES):
            if "audit-prep" not in seen_lead:
                leading.append(("audit-prep", raw)); seen_lead.add("audit-prep")
        elif "audit" in nb:
            if "audit" not in seen_lag:
                lagging.append(("audit", raw)); seen_lag.add("audit")

        # explicit post/remediation phrasing → lagging (label kept distinct)
        for p in AUDIT_LAGGING_PHRASES:
            if p in nb and p not in seen_lag:
                lagging.append((p, raw)); seen_lag.add(p)

        # non-audit leading signals
        for label, phrases in BRANCH_LEADING_SIGNALS:
            if label in seen_lead:
                continue
            if any(p in nb for p in phrases):
                leading.append((label, raw)); seen_lead.add(label)

    return leading, lagging


def fmt_hits(hits):
    """Render lift-hit triples as 'keyword:lift(source); ...' for printing."""
    return "; ".join(f"{k}:{l}({s})" for k, l, s in hits) if hits else "(none)"


def fmt_branch_signals(signals):
    """Render branch signals as 'label@branch; ...'."""
    return "; ".join(f"{label}@{branch}" for label, branch in signals) if signals else "(none)"


# ─── Google Sheets access ─────────────────────────────────────────────────────
def open_spreadsheet(write):
    """Open the spreadsheet. Read-only scope unless we actually intend to write.

    Dry-run still needs READ access (to learn which repos are inserts vs
    updates), so we request the minimal scope that fits the mode.
    """
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


def read_source_repos(ss):
    ws = ss.worksheet(SOURCE_TAB)
    records = ws.get_all_records()  # list of dicts keyed by header row
    repos = []
    for row in records:
        slug = str(row.get("repo", "")).strip()
        eco = str(row.get("ecosystem", "")).strip()
        if slug:
            repos.append({"repo": slug, "ecosystem": eco})
    print(f"✅ Read {len(repos)} repos from '{SOURCE_TAB}'.\n")
    return repos


def load_signals_index(ss):
    """Read radar_signals once. Return (worksheet, {repo: {row_num, values}}).

    row_num is the 1-based sheet row (header is row 1, so first data row is 2).
    values is the existing row padded to the full header width, so we can read
    preserved fields (first_seen, status) by column index safely.
    """
    ws = ss.worksheet(SIGNALS_TAB)
    all_values = ws.get_all_values()
    index = {}
    width = len(SIGNALS_HEADERS)
    for offset, row in enumerate(all_values[1:], start=2):  # skip header, 1-based
        padded = (row + [""] * width)[:width]
        repo = padded[0].strip()
        if repo:
            index[repo] = {"row_num": offset, "values": padded}
    return ws, index


# ─── Row building ─────────────────────────────────────────────────────────────
def hits_to_cell(hits):
    """Evidence cell: 'keyword:lift(source); ...' — empty string when no hits."""
    return "; ".join(f"{k}:{l}({s})" for k, l, s in hits)


def base_fields(result):
    """The columns shared by both signals and history rows."""
    return {
        "repo": result["repo"],
        "repo_url": result["repo_url"],
        "ecosystem": result["ecosystem"],
        "leading_score": result["leading_score"],
        "lagging_score": result["lagging_score"],
        "leading_hits": hits_to_cell(result["leading_hits"]),
        "lagging_hits": hits_to_cell(result["lagging_hits"]),
        "ecosystem_markers": "; ".join(result["ecosystem_markers"]),
        # bonuses not computed yet (parked until after Phase 1) — left blank
        "velocity": "",
        "author_diversity": "",
        "category_breadth": "",
        "has_contract_files": "TRUE" if result["has_sol"] else "FALSE",
        "commits_30d": result["commits_30d"],
        "commits_scanned": result["commits_scanned"],
        "branches_scanned": len(result["branches_scanned"]),
        "pushed_at": result["pushed_at"],
        "default_branch": result["default_branch"],
        "stars": result["stars"],
        "forks": result["forks"],
        "created_at": result["created_at"],
        # branch-name signals (no lift) — persisted as strings, e.g.
        # "audit-prep@refactor/audit-prep; mainnet-deploy@mainnet-deploy"
        "branch_leading": fmt_branch_signals(result.get("branch_leading", [])),
        "branch_lagging": fmt_branch_signals(result.get("branch_lagging", [])),
    }


def build_signal_row(result, first_seen, last_seen, status, cycle_id):
    """Render a radar_signals row in canonical header order."""
    d = base_fields(result)
    d.update({
        "status": status,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "last_cycle_id": cycle_id,
    })
    return [d.get(h, "") for h in SIGNALS_HEADERS]


def build_history_row(result, cycle_id, detected_at):
    """Render a radar_history row in canonical header order."""
    d = base_fields(result)
    d.update({"cycle_id": cycle_id, "detected_at": detected_at})
    return [d.get(h, "") for h in HISTORY_HEADERS]


# ─── GitHub helper with rate-limit + transient-error handling ─────────────────
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


# ─── Branch selection ─────────────────────────────────────────────────────────
def list_branches(slug, headers):
    """Return all branch names for a repo (handles pagination)."""
    names = []
    page = 1
    while True:
        r = github_get(
            f"https://api.github.com/repos/{slug}/branches",
            headers,
            params={"per_page": 100, "page": page},
        )
        if r is None or not r.ok:
            break
        batch = r.json()
        if not batch:
            break
        names.extend(b["name"] for b in batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(API_PACING_SECONDS)
    return names


def select_branches(all_branches, default_branch):
    """Default branch first, then pattern-matched branches by priority, cap N.

    Why default-first: it's the branch GitHub's commit endpoint sees by
    default and the one most teams merge into, so it's the safest single
    signal. The pattern branches are the fallback that catches branch-heavy
    teams whose default branch looks quiet.
    """
    selected = []
    if default_branch and default_branch in all_branches:
        selected.append(default_branch)

    # Rank remaining branches by how early they match a relevant pattern.
    def priority(name):
        low = name.lower()
        for i, pat in enumerate(RELEVANT_BRANCH_PATTERNS):
            if pat in low:
                return i
        return len(RELEVANT_BRANCH_PATTERNS) + 1  # no match → lowest priority

    candidates = sorted(
        (b for b in all_branches if b != default_branch),
        key=lambda b: (priority(b), b.lower()),
    )
    for b in candidates:
        if len(selected) >= MAX_BRANCHES_PER_REPO:
            break
        # Only keep it if it actually matches a relevant pattern.
        if priority(b) <= len(RELEVANT_BRANCH_PATTERNS):
            selected.append(b)

    return selected[:MAX_BRANCHES_PER_REPO]


# ─── Commit fetching + pooling ────────────────────────────────────────────────
def fetch_commits_for_branch(slug, branch, since_iso, headers):
    """Return list of (sha, message) for commits on `branch` since `since_iso`."""
    r = github_get(
        f"https://api.github.com/repos/{slug}/commits",
        headers,
        params={"sha": branch, "since": since_iso, "per_page": 100},
    )
    if r is None or not r.ok:
        return []
    out = []
    for c in r.json():
        sha = c.get("sha", "")
        msg = (c.get("commit", {}).get("message", "") or "").splitlines()[0:1]
        out.append((sha, msg[0] if msg else ""))
    return out


def check_solidity_files(slug, branch, headers):
    """Does the repo contain any .sol files? One call via the git trees API.

    The recursive trees endpoint returns the whole file list in a single
    request — far cheaper than the throttled code-search API. For very large
    repos GitHub truncates the tree; we return that flag so a False is
    reported honestly as "none found (tree truncated)" rather than a hard no.
    Returns (has_sol, sol_count, truncated).
    """
    r = github_get(
        f"https://api.github.com/repos/{slug}/git/trees/{branch}",
        headers,
        params={"recursive": "1"},
    )
    if r is None or not r.ok:
        return (False, 0, False)
    payload = r.json()
    truncated = bool(payload.get("truncated", False))
    sol_count = sum(
        1 for node in payload.get("tree", [])
        if node.get("type") == "blob" and node.get("path", "").lower().endswith(".sol")
    )
    return (sol_count > 0, sol_count, truncated)


def gather_repo(repo, headers, since_iso):
    """Run the front-half pipeline for one repo. Returns a summary dict."""
    slug = repo["repo"]

    # 1. metadata (for default_branch, primary language, existence check)
    meta = github_get(f"https://api.github.com/repos/{slug}", headers)
    if meta is None or meta.status_code == 404:
        return {"repo": slug, "error": "not found (404) — renamed/deleted/private?"}
    if not meta.ok:
        return {"repo": slug, "error": f"metadata HTTP {meta.status_code}"}
    data = meta.json()
    default_branch = data.get("default_branch", "main")
    language = data.get("language") or "(none reported)"
    # context fields for the sheet rows:
    repo_url = data.get("html_url", "")
    stars = data.get("stargazers_count", 0)
    forks = data.get("forks_count", 0)
    created_at = (data.get("created_at") or "")[:10]
    pushed_at = (data.get("pushed_at") or "")[:10]
    time.sleep(API_PACING_SECONDS)

    # 1b. relevance check: primary language + presence of .sol contract files
    has_sol, sol_count, sol_truncated = check_solidity_files(slug, default_branch, headers)
    time.sleep(API_PACING_SECONDS)

    # 2. list + filter branches
    all_branches = list_branches(slug, headers)
    selected = select_branches(all_branches, default_branch)
    time.sleep(API_PACING_SECONDS)

    # 3 + 4. fetch commits per branch, pool + de-dup by SHA.
    # commits_30d = default-branch-only count; commits_scanned = pooled unique.
    pooled = {}  # sha -> message
    commits_30d = 0
    for branch in selected:
        branch_commits = fetch_commits_for_branch(slug, branch, since_iso, headers)
        if branch == default_branch:
            commits_30d = len(branch_commits)
        for sha, msg in branch_commits:
            pooled.setdefault(sha, msg)
        time.sleep(API_PACING_SECONDS)

    # 5. score commit messages AND branch names against the taxonomy.
    # Branch names are FREE (already listed) — score against ALL of them,
    # not just the 5 we fetched commits from.
    all_messages = list(pooled.values())
    leading_score, lagging_score, leading_hits, lagging_hits, markers = score_texts(
        all_messages, all_branches)
    # 5b. branch-name signals with no lift (separate dimension, out of scores)
    branch_leading, branch_lagging = score_branch_signals(all_branches)

    samples = [m for m in pooled.values() if m][:SAMPLE_COMMITS_TO_SHOW]
    return {
        "repo": slug,
        "ecosystem": repo.get("ecosystem", ""),
        "repo_url": repo_url,
        "default_branch": default_branch,
        "language": language,
        "has_sol": has_sol,
        "sol_count": sol_count,
        "sol_truncated": sol_truncated,
        "branches_total": len(all_branches),
        "branches_scanned": selected,
        "commits_30d": commits_30d,
        "commits_scanned": len(pooled),
        "commits_pooled": len(pooled),  # alias kept for the existing printout
        "pushed_at": pushed_at,
        "stars": stars,
        "forks": forks,
        "created_at": created_at,
        "samples": samples,
        "leading_score": leading_score,
        "lagging_score": lagging_score,
        "leading_hits": leading_hits,
        "lagging_hits": lagging_hits,
        "ecosystem_markers": markers,
        "branch_leading": branch_leading,
        "branch_lagging": branch_lagging,
    }


# ─── Built-in scoring sanity check ────────────────────────────────────────────
# Hand-written messages with known expected buckets. This lets us verify the
# scorer's math even when the live repos are quiet (0 commits / off-keyword).
SANITY_CASES = [
    {
        "msg": "add invariant tests and a fuzz test; finalize storage layout before mainnet",
        "expect": "LEADING (invariant, fuzz test, finalize, storage layout) = 13.18; lagging 0",
    },
    {
        "msg": "fix audit finding #3 from the security review; patch CVE",
        "expect": "LAGGING (finding, security review, cve) = 9.28; leading 0",
    },
]


# Branch-name sanity cases: branches only (no commit messages), to verify
# both the lift-in-branch path (source='branch') and the no-lift branch signals.
BRANCH_SANITY_CASES = [
    {
        "branches": ["main", "refactor/audit-prep", "mainnet-deploy"],
        "expect": "branch signals leading: audit-prep, mainnet-deploy; scores stay 0 (no lift)",
    },
    {
        "branches": ["oz-audit", "add-invariant-tests"],
        "expect": "branch signal lagging: audit (bare); LEADING lift invariant:3.05(branch)",
    },
]


def run_sanity_check():
    print("=" * 64)
    print("SCORING SANITY CHECK (hand-written, no network)")
    print("=" * 64)
    for i, case in enumerate(SANITY_CASES, 1):
        lead, lag, lead_hits, lag_hits, _ = score_texts([case["msg"]], [])
        print(f"\nMessage case {i}: \"{case['msg']}\"")
        print(f"   expected : {case['expect']}")
        print(f"   actual   : leading_score={lead}  lagging_score={lag}")
        print(f"              leading hits: {fmt_hits(lead_hits)}")
        print(f"              lagging hits: {fmt_hits(lag_hits)}")

    for i, case in enumerate(BRANCH_SANITY_CASES, 1):
        lead, lag, lead_hits, lag_hits, _ = score_texts([], case["branches"])
        b_lead, b_lag = score_branch_signals(case["branches"])
        print(f"\nBranch case {i}: {case['branches']}")
        print(f"   expected : {case['expect']}")
        print(f"   actual   : leading_score={lead}  lagging_score={lag}")
        print(f"              lift leading hits : {fmt_hits(lead_hits)}")
        print(f"              branch signals lead: {fmt_branch_signals(b_lead)}")
        print(f"              branch signals lag : {fmt_branch_signals(b_lag)}")
    print("\n" + "=" * 64 + "\n")


def print_repo_summary(i, total, result):
    """The per-repo read-out (relevance + commits + scores)."""
    if result.get("error"):
        print(f"[{i}/{total}] {result['repo']}  ⚠️  {result['error']}")
        return
    scanned = result["branches_scanned"]
    if result["sol_count"] > 0:
        sol_str = f"yes ({result['sol_count']} .sol files)"
    elif result["sol_truncated"]:
        sol_str = "none found (file tree truncated — uncertain)"
    else:
        sol_str = "NO .sol files found"
    relevant_flag = "✅" if result["has_sol"] else "⚠️ "

    print(f"[{i}/{total}] {result['repo']}")
    print(f"     {relevant_flag} language : {result['language']}   | contract files: {sol_str}")
    print(f"     default branch   : {result['default_branch']}")
    print(f"     branches scanned : {len(scanned)} of {result['branches_total']} "
          f"→ {', '.join(scanned)}")
    print(f"     commits 30d      : {result['commits_30d']} default-branch | "
          f"{result['commits_scanned']} pooled across branches")
    print(f"     leading_score    : {result['leading_score']}   hits: {fmt_hits(result['leading_hits'])}")
    print(f"     lagging_score    : {result['lagging_score']}   hits: {fmt_hits(result['lagging_hits'])}")
    # Branch-name signals — separate dimension, NOT in the scores above.
    b_lead = result.get("branch_leading", [])
    b_lag = result.get("branch_lagging", [])
    if b_lead or b_lag:
        print(f"     branch signals   : leading: {fmt_branch_signals(b_lead)}  |  "
              f"lagging: {fmt_branch_signals(b_lag)}")


def parse_args():
    p = argparse.ArgumentParser(description="GitHub Radar cycle (Solidity).")
    p.add_argument(
        "--write", action="store_true",
        help="Actually write to radar_signals/radar_history. "
             "Omit for dry-run (preview only).",
    )
    return p.parse_args()


def main():
    args = parse_args()
    mode = "WRITE (will modify the sheet)" if args.write else "DRY-RUN (preview only, no writes)"
    print(f"\nGitHub Radar — cycle, back half wired.   MODE: {mode}\n")

    # Offline sanity check first — proves the scorer regardless of live quiet.
    run_sanity_check()

    token = load_env_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    since_iso = (now_utc() - timedelta(days=COMMIT_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    cycle_id = f"solidity-{now_utc():%Y%m%dT%H%M%SZ}"
    print(f"Activity window: commits since {since_iso} (last {COMMIT_WINDOW_DAYS} days)")
    print(f"Cycle id: {cycle_id}\n")

    # One spreadsheet handle for the whole run. Dry-run still READS signals so
    # the insert-vs-update preview is accurate.
    ss = open_spreadsheet(write=args.write)
    repos = read_source_repos(ss)
    sig_ws, sig_index = load_signals_index(ss)
    hist_ws = ss.worksheet(HISTORY_TAB)
    print(f"radar_signals currently holds {len(sig_index)} repos.\n")

    fs_idx = SIGNALS_HEADERS.index("first_seen")
    st_idx = SIGNALS_HEADERS.index("status")

    inserts = []                 # list of row-lists to append to radar_signals
    updates = []                 # list of (row_num, row-list) for radar_signals
    history_rows = []            # list of row-lists to append to radar_history
    skipped_404 = 0

    for i, repo in enumerate(repos, 1):
        result = gather_repo(repo, headers, since_iso)
        print("─" * 64)
        print_repo_summary(i, len(repos), result)
        if result.get("error"):
            skipped_404 += 1
            print(f"     → SKIP (not written)")
            continue

        slug = result["repo"]
        if slug in sig_index:
            # UPDATE: preserve first_seen + status from the existing row.
            existing = sig_index[slug]["values"]
            preserved_first_seen = existing[fs_idx] or now_iso
            preserved_status = existing[st_idx] or "new"
            row = build_signal_row(
                result,
                first_seen=preserved_first_seen,
                last_seen=now_iso,
                status=preserved_status,
                cycle_id=cycle_id,
            )
            updates.append((sig_index[slug]["row_num"], row))
            print(f"     → would UPDATE signals row {sig_index[slug]['row_num']} "
                  f"(preserve first_seen={preserved_first_seen}, status={preserved_status}; "
                  f"refresh last_seen={now_iso})")
        else:
            # INSERT: brand-new repo.
            row = build_signal_row(
                result,
                first_seen=now_iso,
                last_seen=now_iso,
                status="new",
                cycle_id=cycle_id,
            )
            inserts.append(row)
            print(f"     → would INSERT new signals row (first_seen=last_seen={now_iso}, status=new)")

        # History always gets one append per scanned repo per cycle.
        history_rows.append(build_history_row(result, cycle_id=cycle_id, detected_at=now_iso))
        print(f"     → would APPEND history row (cycle_id={cycle_id})")

    # ── Summary of planned changes ──
    print("─" * 64)
    print(f"\nPlanned writes: {len(inserts)} insert(s), {len(updates)} update(s) to "
          f"radar_signals; {len(history_rows)} append(s) to radar_history. "
          f"({skipped_404} repo(s) skipped on 404.)\n")

    if not args.write:
        print("🟡 DRY-RUN: nothing was written. Re-run with --write to commit.\n")
        return

    # ── Commit ──
    if inserts:
        sig_ws.append_rows(inserts, value_input_option="RAW")
        print(f"✅ Inserted {len(inserts)} new row(s) into {SIGNALS_TAB}.")
    for row_num, row in updates:
        sig_ws.update(range_name=f"A{row_num}", values=[row], value_input_option="RAW")
    if updates:
        print(f"✅ Updated {len(updates)} existing row(s) in {SIGNALS_TAB}.")
    if history_rows:
        hist_ws.append_rows(history_rows, value_input_option="RAW")
        print(f"✅ Appended {len(history_rows)} row(s) to {HISTORY_TAB}.")
    print("\n🎯 Write complete.\n")


if __name__ == "__main__":
    main()
