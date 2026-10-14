"""
staging.py — "scope staging" signal (Radar v2, step 4). SHADOW MODE.

What it detects
---------------
Before asking auditors for quotes, a team often moves the exact code to be
audited into a fresh place (new folders, often a public repo), as finished new
files, in a short burst. Auditors need a frozen, countable scope; this is the
footprint that need leaves behind. The rule was derived from one observed case
and then checked against negative controls before shipping (see
docs/scoring.md).

Rule (all four must hold, in the 14 days before the scan, on the default branch):
  1. new contract folder   >= 1 folder of contract files that didn't exist before
  2. arrival, not rework   >= 75% of contract-file changes are new files
  3. mass                  >= 5 new contract files
  4. novelty share         >= 30% of the repo's contract files are new
Test files, scripts, mocks and vendored folders (lib/, node_modules/...) are ignored.

Tags written for the dashboard (shadow mode: they NEVER change a score):
  staging            all four rules hold in a repo that already existed
  staging:new-repo   all four hold, but the repo was born inside the window
                     (a whole codebase uploaded at once — weaker evidence)
  staging:version    rules 1-3 hold, rule 4 fails, but the repo was quiet for
                     30+ days before the drop (new version folder next to old
                     ones, e.g. src/v2/ appearing beside src/v1/)

Validated before shipping:
  - the case the rule was derived from fires once the new files land, and
    not on the scan before.
  - mature, steadily-maintained libraries used as negative controls never
    fire across a year of weekly checks.
  - on a real candidate pool only a small fraction of repos is tagged — rare
    enough to read every one by hand.

Stdlib only, like the rest of v2. Works on the blobless clone profile.py makes
(file lists are available; file contents are not needed).
"""
import datetime as dt
import os
import re
import subprocess

CONTRACT_EXT = (".sol", ".vy", ".cairo", ".move", ".nr", ".fc", ".func", ".tz")
VENDOR = re.compile(
    r"(^|/)(lib|node_modules|vendor|dependencies|\.deps|out|cache|artifacts|build|third_party)/")
TESTISH = re.compile(
    r"(^|/)(test|tests|testdata|testsuite|mock|mocks|script|scripts|bench|benches|examples?)/"
    r"|\.t\.sol$|\.s\.sol$", re.I)

WINDOW_DAYS = 14
MIN_NEW_DIRS = 1
MIN_ARRIVAL = 0.75
MIN_NEW_FILES = 5
MIN_NOVELTY = 0.30
VERSION_MIN_DORMANT = 30      # days quiet before the drop, for staging:version
MAX_SANE_DORMANT = 3650       # bigger gaps come from broken commit dates; ignore


def _git(path, *args):
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
    return p.stdout


def is_contract(f):
    return f.endswith(CONTRACT_EXT) and not VENDOR.search(f) and not TESTISH.search(f)


def _contract_files_at(path, commit):
    if not commit:
        return []
    return [f for f in _git(path, "ls-tree", "-r", "--name-only", commit).splitlines()
            if is_contract(f)]


def staging(path, scan_date=None):
    """Measure the staging footprint of a cloned repo. Returns a dict of facts
    plus 'tag' ('' when nothing fires)."""
    scan_date = scan_date or dt.date.today()
    end = scan_date + dt.timedelta(days=1)
    start = scan_date - dt.timedelta(days=WINDOW_DAYS)
    now_c = _git(path, "rev-list", "-1", f"--before={end.isoformat()}T00:00:00", "HEAD").strip()
    old_c = _git(path, "rev-list", "-1", f"--before={start.isoformat()}T00:00:00", "HEAD").strip()
    out = dict(tag="", fires=False, born_in_window=not bool(old_c), new_dirs=0, new_files=0,
               contract_files=0, arrival=0.0, novelty=0.0, adders=0, dormant_days=None,
               webui_uploads=0, example_dirs="")
    if not now_c:
        return out

    log = _git(path, "log", now_c, "-M", "--name-status", "--date=short",
               "--format=@@%ad|%ae|%cn|%s", f"--since={start.isoformat()}T00:00:00")
    status = {"A": 0, "M": 0, "D": 0, "R": 0}
    added, adders, webui, first_in = set(), set(), 0, None
    cur = None
    for line in log.splitlines():
        if line.startswith("@@"):
            d, ae, cn, s = (line[2:].split("|", 3) + [""] * 4)[:4]
            cur = (ae.lower(), cn, s.lower())
            first_in = d if first_in is None or d < first_in else first_in
            continue
        if not line.strip() or cur is None:
            continue
        parts = line.split("\t")
        st, f = parts[0][:1], parts[-1]
        if not is_contract(f):
            continue
        if st in status:
            status[st] += 1
        if st == "A":
            added.add(f)
            adders.add(cur[0])
            if cur[1] == "GitHub" and cur[2].startswith("create ") and cur[2].endswith(CONTRACT_EXT):
                webui += 1

    now_files = set(_contract_files_at(path, now_c))
    old_files = set(_contract_files_at(path, old_c))
    new_files = (added & now_files) - old_files
    new_dirs = {os.path.dirname(f) for f in new_files} - {os.path.dirname(f) for f in old_files}
    changes = sum(status.values())
    arrival = status["A"] / changes if changes else 0.0
    novelty = len(new_files) / len(now_files) if now_files else 0.0

    dormant = None
    if old_c and first_in:
        last_before = _git(path, "log", "-1", "--date=short", "--format=%ad", old_c).strip()
        if last_before:
            gap = (dt.date.fromisoformat(first_in) - dt.date.fromisoformat(last_before)).days
            dormant = gap if 0 <= gap <= MAX_SANE_DORMANT else None

    c1 = len(new_dirs) >= MIN_NEW_DIRS
    c2 = arrival >= MIN_ARRIVAL
    c3 = len(new_files) >= MIN_NEW_FILES
    c4 = novelty >= MIN_NOVELTY
    tag = ""
    if c1 and c2 and c3 and c4:
        tag = "staging:new-repo" if not old_c else "staging"
    elif c1 and c2 and c3 and old_c and (dormant or 0) >= VERSION_MIN_DORMANT:
        tag = "staging:version"

    out.update(tag=tag, fires=bool(tag), new_dirs=len(new_dirs), new_files=len(new_files),
               contract_files=len(now_files), arrival=round(arrival, 2),
               novelty=round(novelty, 2), adders=len(adders), dormant_days=dormant,
               webui_uploads=webui, example_dirs=";".join(sorted(new_dirs)[:4]))
    return out


def safe_staging(path, scan_date=None):
    """Never let this experiment break the weekly scan."""
    try:
        return staging(path, scan_date)
    except Exception as e:  # noqa: BLE001 — shadow mode must not crash a run
        return {"tag": "", "fires": False, "error": f"{type(e).__name__}: {e}"[:200]}


# ---------- storage: its own table, so store.py stays untouched ----------

def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staging (
            repo_id      INTEGER NOT NULL,
            scan_date    TEXT    NOT NULL,
            tag          TEXT,
            new_dirs     INTEGER,
            new_files    INTEGER,
            contract_files INTEGER,
            arrival      REAL,
            novelty      REAL,
            adders       INTEGER,
            dormant_days INTEGER,
            webui_uploads INTEGER,
            example_dirs TEXT,
            error        TEXT,
            PRIMARY KEY (repo_id, scan_date)
        )""")


def save(conn, repo_id, scan_date, result):
    if repo_id is None or not result:
        return
    ensure_table(conn)
    scan_date = scan_date or dt.date.today().isoformat()
    if isinstance(scan_date, dt.date):
        scan_date = scan_date.isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO staging VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (repo_id, scan_date, result.get("tag", ""), result.get("new_dirs"),
         result.get("new_files"), result.get("contract_files"), result.get("arrival"),
         result.get("novelty"), result.get("adders"), result.get("dormant_days"),
         result.get("webui_uploads"), result.get("example_dirs"), result.get("error")))
    conn.commit()


def latest_tags(conn):
    """{repo_id: tag} from each repo's most recent staging row, only when it fired.
    Read-only: safe on dashboard.py's read-only connection (no CREATE here)."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='staging'").fetchone()
    if not exists:
        return {}
    rows = conn.execute("""
        SELECT s.repo_id, s.tag FROM staging s
        JOIN (SELECT repo_id, MAX(scan_date) AS d FROM staging GROUP BY repo_id) m
          ON s.repo_id = m.repo_id AND s.scan_date = m.d
        WHERE s.tag != ''""").fetchall()
    return {r[0]: r[1] for r in rows}


if __name__ == "__main__":
    import json
    import sys
    when = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(staging(sys.argv[1], when), indent=1))
