"""
run.py — the daily command.

    python radar/run.py [repo_list.txt]

With no argument it reads data/repos.txt (gitignored). To try it without any
private data, pass examples/repos.sample.txt.

Reads a list of repo URLs, profiles each one, works out what changed since the
last scan, stores it, rebuilds the Obsidian vault, refreshes the HTML
dashboard, and prints a ranked table.

Order per repo is deliberate: profile -> compute_alerts -> save. Alerts are
computed BEFORE today's scan row is written, so "previous" is unambiguously the
last run and never today's own row.

One bad repo never kills the run. A failed clone, a rate-limited API call, a
deleted repo — each prints FAIL and the loop continues, because a scan that
dies on repo 3 of 40 is worse than one that reports 3 failures.
"""
import csv
import datetime
import os
import sys
import time
import traceback

import alert
import dashboard
import graph
import paths
import profile as profile_mod
import store
import staging as staging_mod

DEFAULT_LIST = paths.data_path("repos.txt")

# One row per run, so "is this slow?" becomes a question you can answer from
# data instead of memory. See append_run_history().
HISTORY_PATH = paths.data_path("out", "run_history.csv")
HISTORY_HEADER = ["date", "repos_scanned", "failed", "scan_minutes",
                  "vault_minutes", "dashboard_seconds"]

# How long each phase took, in seconds. scan() fills this in; main() reads it
# to write the history row.
#
# A module-level dict rather than an extra return value, purely so that
# scan()'s 4-tuple signature stays exactly as it was — this change is meant to
# add visibility, not to break anything that already calls scan().
LAST_SCAN_TIMINGS = {}


# --------------------------------------------------------------------------
# progress output
# --------------------------------------------------------------------------

PHASES = 3


def _phase(n, label):
    """A banner so you can see which of the three phases you are in.

    Before this, phase 2 (the vault) printed nothing at all until it finished,
    so a long build was indistinguishable from a hang.
    """
    print(f"\n{'=' * 120}", flush=True)
    print(f"PHASE {n}/{PHASES} — {label}", flush=True)
    print("=" * 120, flush=True)


def _fmt_eta(seconds):
    """Deliberately coarse. An ETA to the second invites you to trust it."""
    if seconds < 90:
        return f"~{max(int(seconds), 1)}s left"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"~{int(round(minutes))} min left"
    h, m = divmod(int(round(minutes)), 60)
    return f"~{h}h {m:02d}m left"


def _eta(about_to_start, total, t0):
    """ETA from the mean time per repo so far.

    `about_to_start` is 1-based and names the repo we are ABOUT to process, so
    the number actually finished is one less. Returns "" for the first repo,
    when there is no average to extrapolate from yet.

    A flat mean, not a decaying one: clone times here are dominated by repo
    size, which is effectively random order, so the mean converges fast and
    a weighted average would mostly track whichever repo happened to be last.
    """
    done = about_to_start - 1
    if done < 1:
        return ""
    elapsed = time.monotonic() - t0
    if elapsed <= 0:
        return ""
    return _fmt_eta((elapsed / done) * (total - done))


def append_run_history(date, repos_scanned, failed, scan_seconds,
                       vault_seconds, dashboard_seconds, path=None):
    """Append one row describing this run; create the file with a header first
    time. Returns the path written, or None.

    Bookkeeping must never sink a run that has already done its real work, so
    every failure here is swallowed into a printed warning.

    repos_scanned counts every repo ATTEMPTED (it matches the [i/N] counter),
    with `failed` broken out separately — so successful repos are the
    difference between the two.
    """
    path = path or HISTORY_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fresh = not os.path.isfile(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if fresh:
                w.writerow(HISTORY_HEADER)
            w.writerow([
                date,
                repos_scanned,
                failed,
                f"{scan_seconds / 60.0:.2f}",
                f"{vault_seconds / 60.0:.2f}",
                f"{dashboard_seconds:.1f}",
            ])
        return path
    except OSError as e:
        print(f"run_history: SKIPPED — {type(e).__name__}: {e}")
        return None


def load_repos(path=None):
    """One URL per line. Blank lines and #-comments ignored, as are trailing
    inline comments so the list can carry expected-score annotations."""
    path = path or DEFAULT_LIST
    if not os.path.isfile(path):
        raise SystemExit(f"no repo list at {path}")
    urls, seen = [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line not in seen:
                seen.add(line)
                urls.append(line)
    return urls


def _fmt_pct(v):
    return "  —  " if v is None else f"{v:>4.0%} "


def _why(prof):
    """Condensed reason string: what fired, buyer first."""
    b = prof.get("buyer_hits") or []
    t = prof.get("timing_hits") or []
    b = b if isinstance(b, list) else [b]
    t = t if isinstance(t, list) else [t]
    return "; ".join([*b, *t]) or "(nothing fired)"


def _truncate(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


# GitHub API calls profile_repo makes per repo: /repos/{o}/{r} and /languages.
CALLS_PER_REPO = 2


def preflight_rate_limit(n_repos):
    """Warn if the GitHub quota will not cover this run. Returns True if it
    warned.

    Warn only, never block: profile.gh_get sleeps through an exhausted quota
    and resumes, so a short quota makes the run slower, not wrong. /rate_limit
    itself is free. Any failure asking is swallowed — a preflight must never
    be the reason a scan does not start.
    """
    try:
        core = profile_mod.gh_get("/rate_limit")["resources"]["core"]
        remaining, limit, reset = core["remaining"], core["limit"], core["reset"]
    except Exception as e:
        print(f"[rate limit] preflight skipped — {type(e).__name__}: {e}",
              flush=True)
        return False

    needed = CALLS_PER_REPO * n_repos
    if remaining >= needed:
        return False

    wait_min = max(0, reset - time.time()) / 60.0
    # One pause to the first reset, then a full hour for every further window
    # the run still needs after that.
    extra_windows = max(0, -(-(needed - remaining) // max(limit, 1)) - 1)
    pause_min = wait_min + 60 * extra_windows
    when = time.strftime("%H:%M", time.localtime(reset))
    print(f"[rate limit] WARNING: {remaining} calls left, this run needs "
          f"~{needed} ({CALLS_PER_REPO} × {n_repos} repos). Quota resets at "
          f"{when}; expect the scan to pause for up to ~{pause_min:.0f} min "
          f"around repo {remaining // CALLS_PER_REPO + 1}/{n_repos}, then "
          f"resume on its own.", flush=True)
    return True


def scan(urls, db_path=None, vault_dir=None, build=True, scan_date=None):
    """Profile every URL, alert, store. Returns (results, failures, alerts)."""
    conn = store.connect(db_path)
    results, failures, fired = [], [], []
    LAST_SCAN_TIMINGS.clear()
    LAST_SCAN_TIMINGS.update(scan_seconds=0.0, vault_seconds=0.0)
    try:
        _phase(1, f"scanning repos  ({len(urls)})")
        t_scan = time.monotonic()
        for i, url in enumerate(urls, 1):
            eta = _eta(i, len(urls), t_scan)
            print(f"[{i}/{len(urls)}]{' ' + eta if eta else ''}   {url}",
                  flush=True)
            profile_mod.PROGRESS = (i, len(urls))
            try:
                prof = profile_mod.profile_repo(url)

                if prof.get("repo_id") is None:
                    raise RuntimeError(
                        prof.get("api_error") or "no repo_id from GitHub API"
                    )

                fired += alert.compute_alerts(
                    prof, conn=conn, scan_date=scan_date
                )
                store.save(prof, conn=conn, scan_date=scan_date)
                try:
                    # Shadow mode: never let staging fail a repo. Saved under
                    # today's date because that is when it was measured.
                    staging_mod.save(conn, prof["repo_id"], None, prof.get("staging"))
                except Exception as e:
                    print(f"[staging] save skipped for {url}: {e}", flush=True)

                prof["status"] = store.get_status(prof["repo_id"], conn=conn)
                results.append(prof)
            except Exception as e:
                print(f"FAIL {url}  —  {type(e).__name__}: {e}", flush=True)
                if os.environ.get("RADAR_TRACE"):
                    traceback.print_exc()
                failures.append((url, f"{type(e).__name__}: {e}"))

        LAST_SCAN_TIMINGS["scan_seconds"] = time.monotonic() - t_scan

        if build:
            _phase(2, "building vault")
            t_vault = time.monotonic()
            vault = graph.build_vault(vault_dir=vault_dir, conn=conn)
            LAST_SCAN_TIMINGS["vault_seconds"] = time.monotonic() - t_vault
        else:
            vault = None
    finally:
        conn.close()

    return results, failures, fired, vault


def print_table(results):
    results = sorted(
        results,
        key=lambda r: (-(r.get("score") or 0), -(r.get("timing") or 0),
                       r.get("full_name") or ""),
    )
    hdr = (f"{'sc':>2} {'buy':>3} {'tim':>3} {'ctr%':>5} "
           f"{'incumbent':<16} {'repo':<44} why")
    print("\n" + "=" * 120)
    print(f"RANKED  ({len(results)} repos)")
    print("=" * 120)
    print(hdr)
    print("-" * 120)
    for r in results:
        name = r.get("full_name") or r.get("url") or "?"
        st = r.get("status")
        if st and st != "new":
            name = f"{name} [{st}]"
        print(
            f"{r.get('score'):>2} {r.get('buyer'):>3} {r.get('timing'):>3} "
            f"{_fmt_pct(r.get('contract_pct'))} "
            f"{_truncate(r.get('incumbent') or '—', 16):<16} "
            f"{_truncate(name, 44):<44} {_truncate(_why(r), 52)}"
        )


def print_alerts(fired):
    print("\n" + "=" * 120)
    if not fired:
        print("ALERTS: none — nothing crossed a threshold since the last scan.")
        print("=" * 120)
        return
    print(f"ALERTS  ({len(fired)})")
    print("=" * 120)
    for a in fired:
        old = "new" if a["old_score"] is None else a["old_score"]
        print(f"  ** {a['kind']:<12} {a['full_name']}   {old} -> {a['new_score']}")
        print(f"     {a['reason']}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else None

    urls = load_repos(path)
    started = datetime.datetime.now()
    print(f"radar v2 · {started:%Y-%m-%d %H:%M} · {len(urls)} repos · db {store.DB_PATH}\n")

    preflight_rate_limit(len(urls))
    results, failures, fired, vault = scan(urls)

    print_table(results)
    print_alerts(fired)

    if failures:
        print("\n" + "=" * 120)
        print(f"FAILED  ({len(failures)})   set RADAR_TRACE=1 for tracebacks")
        print("=" * 120)
        for url, why in failures:
            print(f"  {url}\n     {why}")

    if vault:
        print(f"\nvault: {vault['vault_dir']}  "
              f"({vault['repo_notes']} repos, {vault['people_notes']} people, "
              f"{vault['connectors']} connectors, {vault['edges']} edges)")
        if vault.get("stale_failed"):
            print(f"  WARNING: {vault['stale_failed']} stale note(s) could not be "
                  f"deleted — they will still draw edges in Obsidian")

    # Dashboard last: it reads the DB the scan just wrote, and it is a view,
    # not part of the scan. A broken render must never fail the run — the data
    # is already saved by this point.
    _phase(3, "building dashboard")
    t_dash = time.monotonic()
    try:
        info = dashboard.build()
        dash_seconds = time.monotonic() - t_dash
        print(f"dashboard: {info['path']}  "
              f"({info['repos']} repos, {info['movers']} movers, "
              f"{info['call_today']} to call, {info['bytes'] / 1024:.0f} KB) "
              f"in {dash_seconds:.0f}s")
    except Exception as e:
        dash_seconds = time.monotonic() - t_dash
        print(f"dashboard: SKIPPED — {type(e).__name__}: {e}")

    scan_seconds = LAST_SCAN_TIMINGS.get("scan_seconds", 0.0)
    vault_seconds = LAST_SCAN_TIMINGS.get("vault_seconds", 0.0)

    print(f"\ntiming: scan {scan_seconds / 60:.1f} min · "
          f"vault {vault_seconds / 60:.1f} min · "
          f"dashboard {dash_seconds:.0f}s")

    hist = append_run_history(
        date=f"{started:%Y-%m-%d}",
        repos_scanned=len(urls),
        failed=len(failures),
        scan_seconds=scan_seconds,
        vault_seconds=vault_seconds,
        dashboard_seconds=dash_seconds,
    )
    if hist:
        print(f"history: appended to {hist}")

    print(f"\ndone in {(datetime.datetime.now() - started).total_seconds():.0f}s "
          f"· {len(results)} ok · {len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
