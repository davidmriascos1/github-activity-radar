# Architecture

GitHub Radar answers one question, once a day: **which smart-contract teams
look like they are about to buy a security audit, and what changed since
yesterday?**

It works entirely from public signals — branch names, commit subjects, file
lists, language statistics — and keeps everything it learns in a local SQLite
file.

## The pipeline

```
 public GitHub activity
          │
          ▼
 ┌──────────────────┐   weekly export of repos with recent public events
 │ 1. Discovery     │   (done outside this repo; see "Discovery" below)
 └────────┬─────────┘
          │  thousands of URLs
          ▼
 ┌──────────────────┐   langgate.py — one API call per repo, no clone:
 │ 2. Language gate │   what share of the code is a smart-contract language?
 └────────┬─────────┘
          │  hundreds of URLs
          ▼
 ┌──────────────────┐   profile.py — blobless clone into a temp dir:
 │ 3. Profile       │   full branch list + commit metadata, no file contents
 └────────┬─────────┘
          │
          ▼
 ┌──────────────────┐   score.py      buyer + timing  →  score 1–10
 │ 4. Signals       │   incumbent.py  is an auditor already engaged?
 │                  │   staging.py    was an audit scope just staged? (shadow)
 └────────┬─────────┘
          │
          ▼
 ┌──────────────────┐   alert.py — compare with the previous scan, fire only
 │ 5. Change        │   when something crossed a threshold
 └────────┬─────────┘
          │
          ▼
 ┌──────────────────┐   store.py — SQLite, keyed on GitHub's numeric repo id
 │ 6. Persist       │
 └────────┬─────────┘
          │
          ├──────────────▶  graph.py      contributor graph (Obsidian vault)
          └──────────────▶  dashboard.py  one self-contained HTML triage page
```

`run.py` is the daily command that drives steps 3–6 and then rebuilds the two
views.

## Stage by stage

### 1. Discovery

The radar scores a list of repositories; it does not find them. Candidate
lists come from a periodic export of repositories with recent public activity
(one row per repo: name, event count, first and last event in the window —
see `examples/bq-orgs.sample.csv` for the shape). That export is a query
against a public archive of GitHub events and is run by hand, outside this
repository.

This is the least automated part of the system and the most obvious place for
future work.

### 2. Language gate — `langgate.py`

Discovery is wide and noisy: most active repositories contain no on-chain
code. The gate asks GitHub's `/languages` endpoint for each candidate and
computes

    contract_pct = bytes in contract languages / total bytes

over Solidity, Vyper, Cairo, Move, Noir, FunC and Michelson. It costs one API
call per repo and no clone, so it can run over thousands of URLs in one pass.

The gate **records, it does not reject**. The CSV carries the raw percentage so
a threshold can be chosen afterwards by looking at the borderline cases, rather
than baked in before anyone has seen the distribution.

### 3. Profile — `profile.py`

For each surviving repo: a *blobless, no-checkout* clone. That fetches every
ref and all commit metadata but no file contents, so it usually takes seconds
and still supports `git log --all` and `git branch -r`. The clone lives in a
temp directory and is deleted as soon as the repo is scored.

Two things make this robust enough to run unattended over hundreds of repos:

- **A hard ceiling per clone**, enforced by killing the whole process group.
  `git clone` hands the transfer to helper processes; killing only the parent
  leaves them running. One stuck clone must not block every repo behind it.
- **Rate limits are slept through, not failed on** — but only when the response
  actually carries rate-limit evidence. A 403 from a blocked account or a
  missing scope fails fast instead of sleeping for an hour.

### 4. Signals

Three independent analysers read the same clone.

- **`score.py`** — the score. Two sub-scores of 0–5: *buyer* (is there evidence
  this team buys audits?) and *timing* (is there evidence one is imminent?).
  Detailed in [scoring.md](scoring.md).
- **`incumbent.py`** — if one audit firm is named three or more times, the repo
  probably already has an auditor. Lines that are a package manager talking
  (`forge install …`) are excluded first, because the most-used Solidity
  library shares its name with an audit firm.
- **`staging.py`** — detects a burst of finished new contract files arriving in
  a new folder: the footprint of a team freezing a scope to send out for
  quotes. Runs in **shadow mode**: it writes tags for the dashboard and never
  changes a score.

### 5. Change detection — `alert.py`

A score is a snapshot; an alert is a change. Three kinds, most urgent first:

| Alert | Fires when |
|---|---|
| `window_open` | timing crossed from below 3 to 3 or more — an audit-scoped branch just appeared on a repo already being watched |
| `delta_up` | total score rose by 3 or more since the last scan |
| `new_high` | first scan of this repo, and it came in at 7 or higher |

Alerts are computed *before* today's row is written, so "previous" always means
the last run. Repos marked `contacted`, `client` or `ignore` are suppressed: a
second ping about something already handled trains people to ignore the feed.

### 6. Persistence — `store.py`

SQLite, four tables: `repos`, `scans` (one row per repo per day), `alerts`,
`authors`.

- **Keyed on GitHub's numeric repo id**, never `owner/name`. Repositories get
  renamed and transferred; a rename must not create a phantom second repo or
  reset its history.
- **The upsert is deliberately narrow.** A scan refreshes what GitHub owns
  (name, language, creation date) and never touches the two columns a human
  owns: `status` and `first_seen`. Hand-edits survive every scan.

### Views

- **`graph.py`** writes an Obsidian vault: one note per repo, one per
  contributor, linked. Obsidian's graph view then shows people as the edges
  between projects. A contributor shared between a repo you already work with
  and a repo that just scored high is a warm introduction. Addresses are merged
  conservatively — only when anchored to an observed GitHub noreply handle —
  because a false merge invents a relationship that does not exist.
- **`dashboard.py`** writes one self-contained HTML file (inline CSS and JS, no
  network): *Movers* since the last scan, a *Call today* shortlist (score ≥ 7,
  no incumbent, status `new`), and the full ranked table with filters. It opens
  the database read-only.

## Design choices that shaped the code

**Standard library only.** No `requests`, no ORM, no web framework. The radar
runs from a fresh Python install and has no dependency to break or audit.

**One bad repo never kills a run.** Deleted repos, timeouts and rate limits
each print one `FAIL` line and the loop continues. A scan that dies on repo 3
of 400 is worse than one that reports three failures.

**Views are not part of the scan.** The dashboard and the vault are rebuilt
after the data is saved. A rendering bug cannot lose a day of scanning.

**New signals ship in shadow mode.** `staging.py` records what it sees without
affecting scores until there is enough history to judge it.

**Code and data never share a folder.** Every runtime path resolves under
`data/` (`paths.py`), and `data/` is gitignored.

## Module map

| Module | Role | Network |
|---|---|---|
| `run.py` | daily command, progress, run history | via `profile` |
| `langgate.py` | language gate over a URL list | GitHub API |
| `profile.py` | clone + enrich one repo | git, GitHub API |
| `score.py` | buyer / timing score | none |
| `incumbent.py` | dependency-aware incumbent flag | none |
| `staging.py` | scope-staging signal (shadow) | none |
| `alert.py` | change detection | none |
| `store.py` | SQLite persistence | none |
| `graph.py` | contributor vault | none |
| `dashboard.py` | HTML triage page | none |
| `paths.py` | where runtime data lives | none |
