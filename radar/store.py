"""
store.py — SQLite persistence for Radar v2.

Everything is keyed on the GitHub numeric repo id, never owner/name: repos get
renamed and transferred, and a rename must not create a phantom second repo or
silently reset a status you hand-edited.

The upsert is deliberately narrow. save() refreshes the metadata GitHub owns
(full_name, created_at, contract_pct, primary_lang) and NEVER touches the two
columns you own: `status` and `first_seen`. That is the thing v1's history tab
could not do — your hand-edits survive every scan.

Stdlib only: sqlite3.
"""
import datetime
import os
import sqlite3

import paths

# Default data/out/radar.db (see paths.py). RADAR_DB overrides it — needed
# because sqlite cannot write to a macOS-mounted dir from a Linux sandbox.
DB_PATH = os.environ.get("RADAR_DB") or paths.data_path("out", "radar.db")

# Statuses that mean "stop alerting me about this repo".
SUPPRESSED_STATUSES = ("contacted", "client", "ignore")

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    repo_id      INTEGER PRIMARY KEY,
    full_name    TEXT,
    created_at   TEXT,
    contract_pct REAL,
    primary_lang TEXT,
    first_seen   TEXT,
    status       TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS scans (
    repo_id     INTEGER NOT NULL,
    scan_date   TEXT    NOT NULL,
    buyer       INTEGER,
    timing      INTEGER,
    score       INTEGER,
    buyer_hits  TEXT,
    timing_hits TEXT,
    incumbent   TEXT,
    n_branches  INTEGER,
    n_commits   INTEGER,
    PRIMARY KEY (repo_id, scan_date)
);

CREATE TABLE IF NOT EXISTS alerts (
    repo_id    INTEGER NOT NULL,
    alert_date TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    old_score  INTEGER,
    new_score  INTEGER,
    reason     TEXT,
    PRIMARY KEY (repo_id, alert_date, kind)
);

CREATE TABLE IF NOT EXISTS authors (
    repo_id INTEGER NOT NULL,
    email   TEXT    NOT NULL,
    PRIMARY KEY (repo_id, email)
);

CREATE INDEX IF NOT EXISTS idx_scans_repo  ON scans (repo_id, scan_date DESC);
CREATE INDEX IF NOT EXISTS idx_authors_em  ON authors (email);
"""


def connect(db_path=None):
    """Open the DB, creating parent dirs and schema if needed."""
    path = db_path or DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _today():
    return datetime.date.today().isoformat()


def _joinhits(v):
    """Hit lists are stored as semicolon-joined strings."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return "; ".join(str(x) for x in v)


def splithits(s):
    """Inverse of _joinhits, for readers (graph.py, run.py)."""
    if not s:
        return []
    return [x.strip() for x in s.split(";") if x.strip()]


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------

def save(prof, db_path=None, scan_date=None, conn=None):
    """Upsert the repo row and insert one scan row for `scan_date` (today by
    default). Returns (repo_id, scan_date).

    `scan_date` exists so a caller can backfill or simulate a prior day.

    Raises ValueError if repo_id is missing — we key on the numeric id, and a
    row without one would be unjoinable. Callers doing bulk runs should catch
    this and report the repo as failed rather than storing a broken row.
    """
    repo_id = prof.get("repo_id")
    if repo_id is None:
        raise ValueError(
            f"no repo_id for {prof.get('full_name') or prof.get('url')} "
            "— GitHub API lookup failed; refusing to store an unkeyed row"
        )

    date = scan_date or _today()
    own = conn is None
    conn = conn or connect(db_path)
    try:
        cur = conn.cursor()

        # Upsert. On conflict we refresh GitHub-owned metadata only.
        # status and first_seen are yours — never overwritten.
        cur.execute(
            """
            INSERT INTO repos (repo_id, full_name, created_at, contract_pct,
                               primary_lang, first_seen, status)
            VALUES (?, ?, ?, ?, ?, ?, 'new')
            ON CONFLICT(repo_id) DO UPDATE SET
                full_name    = excluded.full_name,
                created_at   = excluded.created_at,
                contract_pct = COALESCE(excluded.contract_pct, repos.contract_pct),
                primary_lang = COALESCE(excluded.primary_lang, repos.primary_lang)
            """,
            (
                repo_id,
                prof.get("full_name"),
                prof.get("created_at"),
                prof.get("contract_pct"),
                prof.get("primary_lang"),
                date,
            ),
        )

        # One scan row per repo per day. Re-running the same day overwrites
        # that day's scan rather than erroring.
        cur.execute(
            """
            INSERT INTO scans (repo_id, scan_date, buyer, timing, score,
                               buyer_hits, timing_hits, incumbent,
                               n_branches, n_commits)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, scan_date) DO UPDATE SET
                buyer       = excluded.buyer,
                timing      = excluded.timing,
                score       = excluded.score,
                buyer_hits  = excluded.buyer_hits,
                timing_hits = excluded.timing_hits,
                incumbent   = excluded.incumbent,
                n_branches  = excluded.n_branches,
                n_commits   = excluded.n_commits
            """,
            (
                repo_id,
                date,
                prof.get("buyer"),
                prof.get("timing"),
                prof.get("score"),
                _joinhits(prof.get("buyer_hits")),
                _joinhits(prof.get("timing_hits")),
                prof.get("incumbent") or "",
                prof.get("n_branches"),
                prof.get("n_commits"),
            ),
        )

        # Contributor set — replaced wholesale, since people leave teams.
        authors = prof.get("authors") or set()
        cur.execute("DELETE FROM authors WHERE repo_id = ?", (repo_id,))
        cur.executemany(
            "INSERT OR IGNORE INTO authors (repo_id, email) VALUES (?, ?)",
            [(repo_id, e) for e in sorted(authors)],
        )

        conn.commit()
        return repo_id, date
    finally:
        if own:
            conn.close()


def record_alert(repo_id, kind, old_score, new_score, reason,
                 alert_date=None, db_path=None, conn=None):
    """Insert one alert. Idempotent per (repo, date, kind)."""
    date = alert_date or _today()
    own = conn is None
    conn = conn or connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO alerts (repo_id, alert_date, kind, old_score, new_score, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, alert_date, kind) DO UPDATE SET
                old_score = excluded.old_score,
                new_score = excluded.new_score,
                reason    = excluded.reason
            """,
            (repo_id, date, kind, old_score, new_score, reason),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def set_status(repo_id, status, db_path=None, conn=None):
    """Mark a repo contacted/client/ignore. Suppresses future alerts."""
    own = conn is None
    conn = conn or connect(db_path)
    try:
        conn.execute("UPDATE repos SET status = ? WHERE repo_id = ?", (status, repo_id))
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

def previous_score(repo_id, before_date=None, db_path=None, conn=None):
    """Most recent score from a scan STRICTLY BEFORE `before_date` (today by
    default), or None if this repo has never been scanned before then.

    Strictly-before matters: re-running today must compare against yesterday,
    not against the row today's run just wrote.
    """
    date = before_date or _today()
    own = conn is None
    conn = conn or connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT score FROM scans
            WHERE repo_id = ? AND scan_date < ?
            ORDER BY scan_date DESC LIMIT 1
            """,
            (repo_id, date),
        ).fetchone()
        return row["score"] if row else None
    finally:
        if own:
            conn.close()


def previous_scan(repo_id, before_date=None, db_path=None, conn=None):
    """Full prior scan row (needed for the timing-crossing alert), or None."""
    date = before_date or _today()
    own = conn is None
    conn = conn or connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM scans
            WHERE repo_id = ? AND scan_date < ?
            ORDER BY scan_date DESC LIMIT 1
            """,
            (repo_id, date),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def get_repo(repo_id, db_path=None, conn=None):
    own = conn is None
    conn = conn or connect(db_path)
    try:
        row = conn.execute("SELECT * FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def get_status(repo_id, db_path=None, conn=None):
    r = get_repo(repo_id, db_path=db_path, conn=conn)
    return r["status"] if r else None


def all_repos_with_latest(db_path=None, conn=None):
    """Every repo joined to its most recent scan. Feeds graph.py and run.py."""
    own = conn is None
    conn = conn or connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT r.*, s.scan_date, s.buyer, s.timing, s.score,
                   s.buyer_hits, s.timing_hits, s.incumbent,
                   s.n_branches, s.n_commits
            FROM repos r
            LEFT JOIN scans s ON s.repo_id = r.repo_id
            AND s.scan_date = (SELECT MAX(scan_date) FROM scans WHERE repo_id = r.repo_id)
            ORDER BY s.score DESC, r.full_name
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def authors_for(repo_id, db_path=None, conn=None):
    own = conn is None
    conn = conn or connect(db_path)
    try:
        rows = conn.execute(
            "SELECT email FROM authors WHERE repo_id = ? ORDER BY email", (repo_id,)
        ).fetchall()
        return [r["email"] for r in rows]
    finally:
        if own:
            conn.close()


def repos_for_author(email, db_path=None, conn=None):
    own = conn is None
    conn = conn or connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT a.repo_id, r.full_name FROM authors a
            LEFT JOIN repos r ON r.repo_id = a.repo_id
            WHERE a.email = ? ORDER BY r.full_name
            """,
            (email,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    conn = connect()
    print(f"db: {DB_PATH}")
    for t in ("repos", "scans", "alerts", "authors"):
        n = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        print(f"  {t:8} {n} rows")
    conn.close()
