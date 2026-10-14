# V1 retrospective

V1 was the first working version of the radar. It proved the idea was worth
pursuing and showed exactly where the design would not stretch. V2 is a rewrite
informed by running it. This document records what V1 was, what it taught, and
what changed as a result.

The V1 code is preserved, runnable, under [`legacy/v1/`](../legacy/v1/).

## What V1 set out to do

Find smart-contract teams showing signs of preparing for a security audit, by
reading public GitHub activity, and put the result somewhere a non-engineer
could work from it.

It did that in two scripts around a Google Sheet:

- **`radar_filter.py` — the source feed.** Stage A took a roster of Solidity
  repositories from a local open-source ecosystem dataset (no API calls, loose
  filters). Stage B asked the GitHub API which of those were actually active:
  at least 10 commits in the last 60 days. The principle was *permissive at
  source, strict at signal* — popularity is not activity, so stars were only a
  junk floor.
- **`radar_cycle.py` — the scan.** For each repo: list branches, keep the
  default branch plus up to five that matched release/dev patterns, fetch the
  last 30 days of commits on those, and score the commit messages and branch
  names against a keyword taxonomy.
- **The sheet** was the database and the interface at once: a source tab, a
  live signals tab that was upserted each cycle, and an append-only history
  tab.

Scoring used a taxonomy of about fifteen keywords, each with a measured *lift*
value and a bucket: *leading* (getting ready for an audit) or *lagging* (audit
underway or done).

## What worked

- **The premise held.** Audit preparation is visible in public branch names
  and commit messages before a team talks to anyone.
- **Activity over popularity.** Filtering on recent commits instead of stars
  was the right call and carried straight into V2.
- **Evidence next to every score.** V1 wrote the matched keywords beside each
  number. A score nobody can explain does not get acted on; V2 kept this.
- **Preserving human state.** V1 already protected the hand-edited `status`
  column across upserts. V2's storage layer is built around the same rule.
- **A sheet is a fine first interface.** Zero UI work, and the people using the
  output already lived in spreadsheets.

## Where it ran out

**The spreadsheet as a database.** Schema changes were dangerous: adding a
column in the middle would have shifted every existing row, so new columns had
to be appended at the end regardless of where they belonged. Two scripts once
pointed at different sheets and created tabs in the wrong one. Every run needed
a cloud service-account key. And rows were identified by `owner/name`, which
changes when a repository is renamed or transferred.

**Sampling through the API.** Five branches and thirty days is a keyhole. The
branch that matters is often not one that matches a release pattern, and a
repo's first commit date or half-year commit rhythm is not visible in a 30-day
window. Each repo also cost several paced API calls.

**Lift-weighted keywords.** Lift is relative to the pool it was measured in, so
scores could not be compared across ecosystems, and adding a second ecosystem
meant re-measuring everything. Terms with no measured lift earned no points —
which left a branch literally named `audit-prep` outside the score, reported on
a side channel. And substring matching fires inside unrelated words.

**The wrong axis.** Leading/lagging describes where a repo sits in the audit
lifecycle. The question the output had to answer was different: is this team a
buyer, and is the moment now?

**A snapshot, not a signal.** V1 reported the state of each repo. What makes
someone pick up the phone is a change — a branch that appeared since yesterday.
The history tab logged events but nothing computed the difference.

**Operational weight.** Four third-party dependencies, a cloud credential, a
local dataset built by a separate ingestion step, and a smoke test that needed
the live sheet. Nothing could be verified offline.

**Data hygiene.** The sheet identifier lived in source, and code, credentials
and data shared one folder, separated only by a `.gitignore`.

## What was learned

1. **Design around the decision, not the data.** The useful question was never
   "how audit-flavoured is this repo" but "should someone reach out today".
2. **A rule you can read beats a weight you have to trust**, at this data
   volume. With a handful of confirmed outcomes there is not enough signal to
   fit weights honestly.
3. **The strongest evidence is an act, not a word count.** A team creating a
   scoped branch is a discrete event; keyword frequency is a drift.
4. **Get the whole history or accept blind spots.** Sampling made every later
   feature harder.
5. **The interface and the store should not be the same object.**
6. **A new signal is a hypothesis.** It needs negative controls and a period of
   observation before it is allowed to move a score.

## What V2 changed

| | V1 | V2 |
|---|---|---|
| **Data access** | GitHub REST API: 5 branches, 30 days of commits | Blobless git clone: every branch, full commit history, file lists |
| **Storage** | Google Sheet, three tabs | Local SQLite, keyed on GitHub's numeric repo id |
| **Identity** | `owner/name` | Numeric id — survives renames and transfers |
| **Scoring** | Lift-weighted keyword hits; leading vs lagging | Explicit rules; buyer vs timing |
| **Matching** | Case-insensitive substring | Word-boundary regex, flexible hyphens |
| **Output** | Rows in a sheet | Change alerts, a ranked table, an HTML dashboard, a contributor graph |
| **Time** | Snapshot per cycle | Daily scans compared with the previous one |
| **Dependencies** | `gspread`, `google-auth`, `requests`, `duckdb` | Standard library only |
| **Credentials** | GitHub token + cloud service-account key | GitHub token, optional |
| **Failure handling** | Retries on API calls | Per-repo isolation, clone timeouts with process-group kill, rate-limit sleep-and-resume |
| **New signals** | Went straight into the output | Shadow mode first |

### Architecture

One long script that fetched, scored and wrote became small modules with one
job each, most of which never touch the network. The scorer can be run on any
local clone; the dashboard cannot write to the database.

### Scoring

See [scoring.md](scoring.md). In short: two readable sub-scores instead of two
lift sums, every point traceable to a rule, and incumbent detection split out
so that "already has an auditor" is a flag rather than a penalty hidden inside
a number.

### Reproducibility

V1 could only be exercised against live services. In this repository the
scoring core is covered by tests that run offline against synthetic input, and
the scan date is a parameter, so a given history always produces the same
score.

### Data hygiene

All runtime data resolves under one gitignored directory. No identifier,
credential or target list is in source. The only committed datasets are the
synthetic ones in `examples/`.

## What V2 gave up

- **The shared, editable view.** A spreadsheet is collaborative by default; a
  local SQLite file and a static HTML page are single-user.
- **Automated sourcing.** V1's filter produced its own candidate list. In V2,
  discovery is a manual export feeding the language gate.
- **A measured basis for weights.** V1's lift values, whatever their limits,
  came from data. V2's rules are judgment. That trade was deliberate and should
  be revisited once there are enough outcomes to measure against.
