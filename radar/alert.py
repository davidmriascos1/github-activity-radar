"""
alert.py — delta detection.

A score is a snapshot; an alert is a change. This module is what turns the
static number into a signal: it compares today's scan against the last one and
fires only when something moved.

Three kinds, in priority order:

  window_open  timing crossed <3 -> >=3. An audit-scoped branch just appeared
               on a repo you were already watching. THE important one: it says
               a buyer started moving, and it is the only alert tied to a
               specific observable act rather than to a number drifting.
  delta_up     score jumped >= DELTA_MIN since the last scan.
  new_high     first time we ever scanned this repo and it came in >= HIGH_MIN.

Repos whose status is contacted/client/ignore are suppressed — you already
know about them, and a second ping is noise that trains you to ignore the feed.

Stdlib only.
"""
import store

HIGH_MIN = 7      # "new_high" threshold on a first-ever scan
DELTA_MIN = 3     # score jump that counts as "delta_up"
TIMING_OPEN = 3   # timing at/above this = an audit window is open

# Most urgent first. run.py prints in this order.
KIND_PRIORITY = ("window_open", "delta_up", "new_high")


class Alert(dict):
    """A fired alert. dict so it serialises trivially; attributes for reading."""

    def __init__(self, repo_id, full_name, kind, old_score, new_score, reason):
        super().__init__(
            repo_id=repo_id, full_name=full_name, kind=kind,
            old_score=old_score, new_score=new_score, reason=reason,
        )

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def __repr__(self):
        old = "—" if self["old_score"] is None else self["old_score"]
        return f"<{self['kind']} {self['full_name']} {old}->{self['new_score']}: {self['reason']}>"


def _priority(kind):
    return KIND_PRIORITY.index(kind) if kind in KIND_PRIORITY else len(KIND_PRIORITY)


def compute_alerts(prof, db_path=None, conn=None, scan_date=None, persist=True):
    """Compare `prof` against this repo's previous scan and return fired alerts.

    Call this BEFORE store.save() for today. It compares against scans strictly
    before `scan_date`, so it is correct in either order — but running it first
    keeps the "previous" reading unambiguous if you re-run a day.

    Returns [] for suppressed repos, for repos with no repo_id, and when
    nothing moved.
    """
    repo_id = prof.get("repo_id")
    if repo_id is None:
        return []

    own = conn is None
    conn = conn or store.connect(db_path)
    try:
        date = scan_date or store._today()

        # Suppression first — cheapest check, and it short-circuits everything.
        repo_row = store.get_repo(repo_id, conn=conn)
        status = (repo_row or {}).get("status")
        if status in store.SUPPRESSED_STATUSES:
            return []

        new_score = prof.get("score")
        new_timing = prof.get("timing")
        prev = store.previous_scan(repo_id, before_date=date, conn=conn)

        alerts = []

        if prev is None:
            # Never seen before. An open audit window is an observable act, not
            # a drift — it is meaningful without a baseline, so it fires on
            # first sight. A hot repo on its first scan is the most valuable
            # alert we can produce, not the least.
            if new_timing is not None and new_timing >= TIMING_OPEN:
                hits = prof.get("timing_hits") or []
                why = "; ".join(hits) if not isinstance(hits, str) else hits
                alerts.append(Alert(
                    repo_id, prof.get("full_name"), "window_open", None, new_score,
                    f"first scan with timing {new_timing} (>= {TIMING_OPEN}) "
                    f"[{why or 'no hits recorded'}]",
                ))

            if new_score is not None and new_score >= HIGH_MIN:
                alerts.append(Alert(
                    repo_id, prof.get("full_name"), "new_high", None, new_score,
                    f"first scan at {new_score} (>= {HIGH_MIN}); "
                    f"buyer {prof.get('buyer')} / timing {new_timing}",
                ))
        else:
            old_score = prev.get("score")
            old_timing = prev.get("timing")

            # The window opening is the act we actually care about.
            if (old_timing is not None and new_timing is not None
                    and old_timing < TIMING_OPEN <= new_timing):
                hits = prof.get("timing_hits") or []
                why = "; ".join(hits) if not isinstance(hits, str) else hits
                alerts.append(Alert(
                    repo_id, prof.get("full_name"), "window_open", old_score, new_score,
                    f"timing {old_timing} -> {new_timing} since {prev.get('scan_date')}"
                    f" [{why or 'no hits recorded'}]",
                ))

            if (old_score is not None and new_score is not None
                    and new_score - old_score >= DELTA_MIN):
                alerts.append(Alert(
                    repo_id, prof.get("full_name"), "delta_up", old_score, new_score,
                    f"score {old_score} -> {new_score} "
                    f"(+{new_score - old_score}) since {prev.get('scan_date')}",
                ))

        alerts.sort(key=lambda a: _priority(a["kind"]))

        if persist:
            for a in alerts:
                store.record_alert(
                    a["repo_id"], a["kind"], a["old_score"], a["new_score"],
                    a["reason"], alert_date=date, conn=conn,
                )

        return alerts
    finally:
        if own:
            conn.close()


def recent_alerts(days=7, db_path=None, conn=None):
    """Alerts fired in the last `days`, newest first. For run.py's footer."""
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    own = conn is None
    conn = conn or store.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT a.*, r.full_name, r.status FROM alerts a
            LEFT JOIN repos r ON r.repo_id = a.repo_id
            WHERE a.alert_date >= ?
            ORDER BY a.alert_date DESC, a.kind
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    for a in recent_alerts(30):
        print(f"{a['alert_date']}  {a['kind']:12} {a['full_name']}  "
              f"{a['old_score']}->{a['new_score']}  {a['reason']}")
