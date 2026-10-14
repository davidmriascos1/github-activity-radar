"""
graph.py — the contributor graph, as a plain Obsidian vault.

An Obsidian vault is just markdown files with [[wikilinks]]; Obsidian's graph
view does the rest. No install, no dependency, nothing to keep in sync.

This is deliberately NOT graphify. Graphify maps code structure inside one
repo. What we need is the opposite axis: repos and people ACROSS repos, where a
shared human is the edge. A contributor who committed to a past client and to a
live prospect is the warmest lead the system can produce, and in this vault
they are a literal node joining the two.

Two note types:
    repos/{name}.md    frontmatter (score/buyer/timing/status/incumbent),
                       buyer hits, and a [[link]] per contributor
    people/{email}.md  a [[link]] per repo that person committed to

Regenerated from SQLite on every run. Stdlib only.
"""
import os
import re
import shutil
import sys
import time
from collections import defaultdict

import paths
import store

DEFAULT_VAULT = paths.data_path("out", "vault")

# Characters Obsidian cannot carry inside a [[wikilink]], plus path separators.
_UNSAFE = re.compile(r'[\[\]|#^/\\:*?"<>]+')


GITHUB_NOREPLY = "users.noreply.github.com"

# Local-parts too generic to fold on. Without this, a single
# 'dev@users.noreply.github.com' anywhere would swallow every 'dev@...' in the
# corpus and produce exactly the hub node the bot denylist exists to prevent.
GENERIC_LOCALS = {
    "dev", "admin", "info", "git", "me", "hello", "hi", "mail", "email",
    "contact", "team", "support", "noreply", "no-reply", "user", "test",
    "root", "build", "ci", "bot", "os", "eng", "engineering", "security",
}


# --------------------------------------------------------------------------
# progress reporting
# --------------------------------------------------------------------------
#
# The vault build is the quietest expensive step in the pipeline. It writes one
# small file per repo and per person — tens of thousands of them — and until
# now printed nothing at all between "scanning" and the final summary. A run
# that sat silent for ten minutes was indistinguishable from a hung one, which
# is the entire reason this section exists.
#
# Nothing here changes what gets written. It is output only.

BAR_WIDTH = 15          # characters in the bar itself
PLAIN_EVERY = 1000      # non-TTY: emit one plain line per N notes
_MIN_REDRAW = 0.1       # seconds between in-place redraws


def _fmt_secs(s):
    """Coarse on purpose — this is reassurance, not a stopwatch."""
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class _Progress:
    """One counter, rendered two ways.

    On a terminal it is a single line redrawn in place with \\r, so the vault
    build occupies one row no matter how many notes it writes. When stdout is
    redirected to a file, \\r would produce one enormous unreadable line, so it
    degrades to a plain line every `plain_every` notes instead.

    Writes to stdout deliberately, not stderr: the fallback exists so that a
    redirected log stays informative, and that only works if the progress goes
    into the same stream being redirected.
    """

    def __init__(self, total, noun, stream=None, width=BAR_WIDTH,
                 plain_every=PLAIN_EVERY, enabled=True):
        self.total = max(int(total or 0), 0)
        self.noun = noun
        self.stream = stream if stream is not None else sys.stdout
        self.width = width
        self.plain_every = max(int(plain_every), 1)
        self.enabled = enabled
        self.n = 0
        self.started = time.monotonic()
        self._last_draw = 0.0
        self._last_plain = 0
        try:
            self.tty = bool(self.stream.isatty())
        except Exception:
            # A stream that cannot answer isatty() (some capture harnesses)
            # gets the safe, redirect-friendly rendering.
            self.tty = False

    # -- rendering ---------------------------------------------------------

    def _pct(self):
        return 1.0 if not self.total else min(self.n / self.total, 1.0)

    def _bar(self):
        filled = int(round(self._pct() * self.width))
        return "█" * filled + "░" * (self.width - filled)

    def _line(self):
        return (f"vault: {self._bar()}  {self.n:,} / {self.total:,} "
                f"{self.noun} ({self._pct():.0%})")

    def _write(self, s):
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:
            # Progress output must never be able to kill a build that is
            # otherwise succeeding.
            self.enabled = False

    # -- api ---------------------------------------------------------------

    def start(self):
        if self.enabled and not self.tty:
            self._write(f"vault: writing {self.total:,} {self.noun}…\n")
        return self

    def tick(self, k=1):
        self.n += k
        if not self.enabled:
            return
        if self.tty:
            now = time.monotonic()
            # Throttle: redrawing 13,500 times costs more than the work.
            if now - self._last_draw < _MIN_REDRAW and self.n < self.total:
                return
            self._last_draw = now
            self._write("\r" + self._line() + "\033[K")
        elif self.n - self._last_plain >= self.plain_every:
            self._last_plain = self.n
            self._write(f"vault:   {self.n:,} / {self.total:,} {self.noun} "
                        f"({self._pct():.0%})\n")

    def close(self):
        if not self.enabled:
            return
        took = _fmt_secs(time.monotonic() - self.started)
        if self.tty:
            self._write("\r" + self._line() + f"  in {took}\033[K\n")
        else:
            self._write(f"vault:   {self.n:,} / {self.total:,} {self.noun} "
                        f"(100%) in {took}\n")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False


def _note(msg, enabled=True):
    """A one-off progress line for steps too short to warrant a bar."""
    if enabled:
        print(f"vault: {msg}", flush=True)


def _noreply_handle(email):
    """GitHub noreply -> the handle, in both shapes:
        12345+handle@users.noreply.github.com -> handle
        handle@users.noreply.github.com       -> handle
    Returns None for anything else."""
    e = (email or "").strip().lower()
    if not e.endswith("@" + GITHUB_NOREPLY):
        return None
    local = e.split("@", 1)[0]
    if "+" in local:
        local = local.split("+", 1)[1]
    return local or None


def canonical_identities(emails):
    """Collapse one human's several git identities into one node.

    Anchored on GitHub handles: a handle is only treated as an identity if it
    was actually observed as a noreply address. Then any other address whose
    local-part equals that handle folds into it.

    The anchor requirement is the safety rail. Merging purely on matching
    local-part would fuse alice@example.com with alice@example.org — two humans,
    one false edge. Under-merging costs a missed link; over-merging invents a
    warm lead that does not exist, so the bias is deliberately conservative.

    Returns (alias_map, members):
        alias_map: email -> canonical id
        members:   canonical id -> sorted list of the emails folded into it
    """
    emails = {(e or "").strip().lower() for e in emails if e and e.strip()}

    # 1. Which handles did we actually see as a GitHub noreply address?
    anchors = set()
    for e in emails:
        h = _noreply_handle(e)
        if h and h not in GENERIC_LOCALS:
            anchors.add(h)

    # 2. Fold.
    alias_map = {}
    for e in emails:
        h = _noreply_handle(e)
        if h and h in anchors:
            alias_map[e] = h
            continue
        local = e.split("@", 1)[0]
        if "+" in local:
            local = local.split("+", 1)[1]
        if local in anchors and local not in GENERIC_LOCALS:
            alias_map[e] = local
        else:
            alias_map[e] = e

    members = defaultdict(list)
    for e, c in alias_map.items():
        members[c].append(e)
    return alias_map, {c: sorted(v) for c, v in members.items()}


def _slug(s):
    """Filesystem- and wikilink-safe. Must be stable: the people notes link to
    repo notes by this exact string, so any change breaks every edge."""
    s = _UNSAFE.sub("-", str(s)).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return s or "unknown"


def _repo_slugs(repos):
    """Prefer the bare repo name ('example-protocol') — the graph is far more
    readable. Fall back to 'owner-repo' only for names that collide, so two
    different orgs with a repo called 'core' stay distinct nodes."""
    by_name = defaultdict(list)
    for r in repos:
        full = r.get("full_name") or str(r["repo_id"])
        name = full.split("/")[-1]
        by_name[name].append(r["repo_id"])

    slugs = {}
    for r in repos:
        full = r.get("full_name") or str(r["repo_id"])
        owner, _, name = full.rpartition("/")
        if len(by_name[name]) > 1 and owner:
            slugs[r["repo_id"]] = _slug(f"{owner}-{name}")
        else:
            slugs[r["repo_id"]] = _slug(name)
    return slugs


def _yaml_str(v):
    """Quote anything YAML might misread (empty, colons, leading specials)."""
    if v is None:
        return '""'
    s = str(v)
    if s == "" or re.search(r'[:#\[\]{}",]', s) or s[:1] in "-?&*!|>%@`":
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _clean_dir(d, keep):
    """Remove generated .md files that no longer correspond to a row. Notes are
    regenerated every run — put hand annotations somewhere other than
    repos/ and people/."""
    if not os.path.isdir(d):
        return 0, 0
    removed = failed = 0
    for fn in os.listdir(d):
        if fn.endswith(".md") and fn not in keep:
            try:
                os.remove(os.path.join(d, fn))
                removed += 1
            except OSError:
                # Don't swallow this silently — a vault that cannot drop stale
                # notes will keep drawing edges from repos you removed.
                failed += 1
    return removed, failed


def build_vault(vault_dir=None, db_path=None, conn=None, min_score_tag=7,
                progress=True):
    """Read repos + authors from SQLite, write the vault. Returns a summary dict.

    `progress` controls the progress output only; it has no effect on what is
    written. Pass False to silence it (tests, or when calling this as a
    library).
    """
    vault_dir = vault_dir or DEFAULT_VAULT
    repos_dir = os.path.join(vault_dir, "repos")
    people_dir = os.path.join(vault_dir, "people")
    os.makedirs(repos_dir, exist_ok=True)
    os.makedirs(people_dir, exist_ok=True)

    own = conn is None
    conn = conn or store.connect(db_path)
    try:
        _note("reading repos from the database…", progress)
        repos = store.all_repos_with_latest(conn=conn)
        slugs = _repo_slugs(repos)
        by_id = {r["repo_id"]: r for r in repos}

        # repo -> [emails], collapsed so one human is one node.
        # One query per repo, so this is worth a bar of its own: on a large db
        # it is a visible slice of the "silent" time, before a single note has
        # been written.
        raw_authors = {}
        with _Progress(len(repos), "repos read", enabled=progress) as bar:
            for r in repos:
                raw_authors[r["repo_id"]] = store.authors_for(r["repo_id"], conn=conn)
                bar.tick()

        _note("folding duplicate git identities…", progress)
        alias_map, members = canonical_identities(
            e for emails in raw_authors.values() for e in emails
        )
        repo_authors = {
            rid: sorted({alias_map.get(e, e) for e in emails})
            for rid, emails in raw_authors.items()
        }

        author_repos = defaultdict(list)
        for rid, ids in repo_authors.items():
            for e in ids:
                author_repos[e].append(rid)

        # Which repos share a human with which — the murmuration edge.
        shared = defaultdict(lambda: defaultdict(list))
        for email, rids in author_repos.items():
            if len(rids) < 2:
                continue
            for a in rids:
                for b in rids:
                    if a != b:
                        shared[a][b].append(email)

        repo_files, people_files = set(), set()

        # ---- repo notes ----
        repo_bar = _Progress(len(repos), "repo notes", enabled=progress).start()
        for r in repos:
            rid = r["repo_id"]
            slug = slugs[rid]
            emails = repo_authors.get(rid, [])
            score = r.get("score")

            tags = ["radar"]
            if r.get("status"):
                tags.append(f"status/{_slug(r['status'])}")
            if score is not None and score >= min_score_tag:
                tags.append("hot")
            if r.get("incumbent"):
                tags.append("incumbent")

            L = [
                "---",
                f"score: {score if score is not None else 'null'}",
                f"buyer: {r.get('buyer') if r.get('buyer') is not None else 'null'}",
                f"timing: {r.get('timing') if r.get('timing') is not None else 'null'}",
                f"status: {_yaml_str(r.get('status') or 'new')}",
                f"incumbent: {_yaml_str(r.get('incumbent') or '')}",
                f"contract_pct: {r.get('contract_pct') if r.get('contract_pct') is not None else 'null'}",
                f"primary_lang: {_yaml_str(r.get('primary_lang') or '')}",
                f"repo_id: {rid}",
                f"full_name: {_yaml_str(r.get('full_name') or '')}",
                f"created_at: {_yaml_str(r.get('created_at') or '')}",
                f"last_scan: {_yaml_str(r.get('scan_date') or '')}",
                "tags: [" + ", ".join(tags) + "]",
                "---",
                "",
                f"# {r.get('full_name') or slug}",
                "",
                f"**Score {score}** (buyer {r.get('buyer')} / timing {r.get('timing')})"
                f" · status `{r.get('status') or 'new'}`",
                "",
                f"https://github.com/{r.get('full_name')}" if r.get("full_name") else "",
                "",
                "## Why",
                "",
                f"- **Buyer:** {'; '.join(store.splithits(r.get('buyer_hits'))) or '(none)'}",
                f"- **Timing:** {'; '.join(store.splithits(r.get('timing_hits'))) or '(none)'}",
                f"- **Incumbent:** {r.get('incumbent') or '(none)'}",
                f"- **Language:** {r.get('primary_lang') or '?'}"
                + (f" · contract {r['contract_pct']:.0%}" if r.get("contract_pct") is not None else ""),
                f"- **Activity:** {r.get('n_branches')} branches / {r.get('n_commits')} commits",
                "",
                f"## Contributors ({len(emails)})",
                "",
            ]

            L.append(" · ".join(f"[[{_slug(e)}]]" for e in emails) if emails else "_none recorded_")
            L.append("")

            # The payload: who else these people work with.
            if shared.get(rid):
                L += ["## Shared contributors", "",
                      "_Repos linked to this one by a shared human._", ""]
                for other, emails_shared in sorted(
                    shared[rid].items(), key=lambda kv: -len(kv[1])
                ):
                    o = by_id.get(other)
                    if not o:
                        continue
                    who = ", ".join(f"[[{_slug(e)}]]" for e in sorted(emails_shared))
                    L.append(
                        f"- [[{slugs[other]}]] (score {o.get('score')}, "
                        f"status `{o.get('status') or 'new'}`) — {len(emails_shared)} shared: {who}"
                    )
                L.append("")

            fn = f"{slug}.md"
            repo_files.add(fn)
            with open(os.path.join(repos_dir, fn), "w", encoding="utf-8") as fh:
                fh.write("\n".join(x for x in L if x is not None).rstrip() + "\n")
            repo_bar.tick()
        repo_bar.close()

        # ---- people notes ----
        # The big one: one note per distinct human across every tracked repo,
        # which is where the run used to go quiet for minutes at a time.
        people_bar = _Progress(len(author_repos), "people notes",
                               enabled=progress).start()
        for email, rids in sorted(author_repos.items()):
            eslug = _slug(email)
            rows = [by_id[x] for x in rids if x in by_id]
            rows.sort(key=lambda x: (-(x.get("score") or 0), x.get("full_name") or ""))
            top = max((x.get("score") or 0) for x in rows) if rows else 0

            tags = ["person"]
            if len(rows) > 1:
                tags.append("connector")  # appears in more than one repo
            statuses = {(x.get("status") or "new") for x in rows}
            if statuses & set(store.SUPPRESSED_STATUSES):
                tags.append("touches-known")  # worked on a repo you've engaged

            aliases = members.get(email, [email])

            L = [
                "---",
                f"identity: {_yaml_str(email)}",
                f"aliases: [" + ", ".join(_yaml_str(a) for a in aliases) + "]",
                f"repo_count: {len(rows)}",
                f"top_score: {top}",
                "tags: [" + ", ".join(tags) + "]",
                "---",
                "",
                f"# {email}",
                "",
                f"Committed to **{len(rows)}** tracked repo(s).",
                "",
            ]
            if len(aliases) > 1:
                L += [f"_Merged git identities: {', '.join('`'+a+'`' for a in aliases)}_", ""]
            for x in rows:
                L.append(
                    f"- [[{slugs[x['repo_id']]}]] — score {x.get('score')}, "
                    f"status `{x.get('status') or 'new'}`"
                )
            L.append("")

            fn = f"{eslug}.md"
            people_files.add(fn)
            with open(os.path.join(people_dir, fn), "w", encoding="utf-8") as fh:
                fh.write("\n".join(L).rstrip() + "\n")
            people_bar.tick()
        people_bar.close()

        # Scans both note directories, so on a large vault this is another
        # stretch of silence worth announcing.
        _note("removing stale notes…", progress)
        r_ok, r_bad = _clean_dir(repos_dir, repo_files)
        p_ok, p_bad = _clean_dir(people_dir, people_files)
        stale, stale_failed = r_ok + p_ok, r_bad + p_bad

        connectors = {e: r for e, r in author_repos.items() if len(r) > 1}
        return {
            "vault_dir": vault_dir,
            "repo_notes": len(repo_files),
            "people_notes": len(people_files),
            "connectors": len(connectors),
            "identities_merged": sum(len(v) - 1 for v in members.values() if len(v) > 1),
            "edges": sum(len(v) for v in shared.values()) // 2,
            "stale_removed": stale,
            "stale_failed": stale_failed,
        }
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import sys
    out = build_vault(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"vault      : {out['vault_dir']}")
    print(f"repo notes : {out['repo_notes']}")
    print(f"people     : {out['people_notes']}")
    print(f"connectors : {out['connectors']} (people in >1 repo)")
    print(f"merged     : {out['identities_merged']} duplicate git identit(ies) folded")
    print(f"edges      : {out['edges']} repo-pairs sharing a human")
    if out["stale_removed"]:
        print(f"stale      : {out['stale_removed']} removed")
    if out["stale_failed"]:
        print(f"WARNING    : {out['stale_failed']} stale note(s) could not be "
              f"deleted (permissions) — they will still draw edges in Obsidian")
