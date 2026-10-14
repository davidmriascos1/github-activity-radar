# Scoring

How the radar turns a repository's public history into a number, and how that
method changed. No real repositories are named here; the worked example at the
end is the synthetic repo built by `examples/offline_demo.py`.

## The idea

A team preparing for an audit leaves traces in public before it contacts
anyone: it names an auditor in a commit, opens a branch called `pre-audit`,
freezes a version folder, commits in a crunch. None of these is proof. Together
they are a decent reason to make one call today instead of next month.

The scorer asks two separate questions and adds the answers.

| Sub-score | Question | Range |
|---|---|---|
| **Buyer** | Is there evidence this team buys audits at all? | 0–5 |
| **Timing** | Is there evidence one is close? | 0–5 |

`score = buyer + timing`, clamped to 1–10.

Keeping them apart matters. A repo that has named three audit firms over two
years is a known buyer with no urgency. A four-day-old repo with a
`code-freeze` branch is urgent with no track record. A single blended number
hides which of the two you are looking at; the dashboard shows both.

## Buyer (0–5)

Read from branch names **and** commit subjects. Some teams name their auditor
only in commit messages and never in a branch, so both sources count.

| Evidence | Points |
|---|---|
| An audit firm is named (any number of firms) | +2 |
| An AI-audit keyword (`ai-audit`, `llm-audit`, `agent-audit`, …) | +2 |
| Remediation language (`audit-fixes`, `post-audit`, `remediation`, …) | +1 |

## Timing (0–5)

| Evidence | Points | Source |
|---|---|---|
| An audit-scoped branch exists | +3 | branch names only |
| The repo's first commit is less than 180 days old | +1 | commit dates |
| A commit-velocity spike in the last 12 weeks | +1 | commit dates |

**Audit-scoped branch.** Either an explicit keyword (`pre-audit`,
`audit-prep`, `audit-scope`, `code-freeze`, …) or a structural match: a branch
whose name *ends* in `-audit`, such as `feat/v0.6.0-audit`. A branch called
just `audit` is ignored. Many repos keep one permanently; it says nothing about
now.

**Velocity spike.** Weekly commit counts over the last 26 weeks, anchored on
the most recent commit. The baseline is the median of the older half (weeks −26
to −14). A spike is any of the last 13 weeks exceeding twice that baseline. If
the baseline is zero, a single week with five or more commits counts — a burst
from cold.

Timing is deliberately weighted toward the branch. It is the only signal tied
to a specific, observable act rather than to a number drifting.

## Incumbent

Separate from the score. If one firm is named three or more times across
commits and branches, the repo is flagged with that firm and the count
(`halborn x3`). The dashboard uses it to keep repos that already have an
auditor off the "call today" list.

`incumbent.py` recomputes the flag after removing lines that are a package
manager talking — `forge install`, `npm install`, dependency bumps, lockfile
changes, `lib/` paths. The reason is one specific collision: the most widely
used Solidity library shares its name with an audit firm, so five package
installs used to read as "this team already has an auditor". The error is
asymmetric. A false incumbent flag hides a real lead; a missed one costs a
single conversation. The filter changes the flag only — buyer and timing are
untouched, so no score moves.

## Keyword matching

Every keyword is compiled to a regular expression with word boundaries, so
`oz` matches `fix/oz-fee-order` and not `frozen`. A hyphen in a keyword also
matches a space, an underscore or nothing: `pre-audit` catches `pre audit`,
`pre_audit` and `preaudit`.

One keyword is exempt. `ai-review` must appear with its hyphen, because
"address ai review comments" is how developers talk about AI code-review bots,
which has nothing to do with a security audit.

Bots are excluded from the contributor set (dependency bots, CI accounts,
coding agents) but their commits still count as activity.

## Evolution

### V1 — lift-weighted keywords

The first implementation (see [v1-retrospective.md](v1-retrospective.md))
matched a taxonomy of about fifteen keywords as substrings of commit messages
and branch names. Each keyword carried a *lift* value measured against a
reference pool of repositories, and fell into a *leading* bucket (getting ready) or a *lagging* one (audit underway
or finished). A repo got a leading score and a lagging score.

It was principled, and its limits were instructive:

- Lift is relative to the pool it was measured in. The numbers were valid for
  one ecosystem and could not be compared across ecosystems.
- Terms with no measured lift earned nothing. A branch literally named
  `audit-prep` had to be reported on a side channel because no lift existed for
  it — the strongest signal in the data sat outside the score.
- Substring matching fires inside longer words.
- Leading vs lagging describes *where in the audit lifecycle* a repo is. The
  decision the score has to support is different: *is this a buyer, and is it
  now?*

### V2.0 — buyer and timing

A redesign around the decision. Firm names, AI-audit terms and remediation
language became the buyer score; scoped branches, repo age and velocity became
the timing score. Rules replaced lift values: every point is traceable to a
line anyone can read, and the reason string printed next to each score
(`firms:…; audit-scoped-branch; velocity-crunch`) is the full explanation.

The scorer also moved from sampled API data — a capped number of branches and a
30-day commit window — to the complete branch list and commit history of a
blobless clone.

### V2.1 — hyphen fix

Keywords written with a hyphen were only matching the hyphenated spelling.
They now match the spaced, underscored and joined forms as well. `ai-review`
was made strict after the looser match produced false hits on AI code-review
chatter.

### V2.2 — a longer firm list, tested

The audit-firm list roughly tripled. The additions were tested against real
commit text, and two were removed again because they
are ordinary words or phrases in this domain and fired on everyday commits. A
keyword list is only as good as its false-positive rate; adding names is cheap
and each one is a liability until tested.

### V2.3 — the age rule uses the real scan date

The "younger than 180 days" rule had been comparing against a date frozen at
development time, so it drifted further from the truth every day. It now uses
the date of the scan by default; passing a date explicitly keeps tests and
re-scoring of a past day reproducible.

### Dependency-aware incumbent

The filter described above, added after a repo with no auditor at all was
flagged as having one.

### Scope staging — shadow mode

The newest signal, in `staging.py`. Before asking auditors for quotes, a team
often moves the exact code to be audited into a fresh place as finished new
files, in a short burst, because auditors price a frozen, countable scope. The
rule requires all four of the following in the 14 days before the scan:

1. at least one new folder of contract files
2. at least 75% of contract-file changes are additions, not edits
3. at least five new contract files
4. the new files are at least 30% of the repo's contract files

Tests, scripts, mocks and vendored code are ignored. Three tags distinguish a
normal hit, a repo born inside the window (weaker: a whole codebase uploaded at
once), and a new version folder appearing next to old ones after a quiet
period.

It was derived from one observed case and then checked against mature,
steadily-maintained libraries as negative controls, where it never fired. On a
real candidate pool it tags only a small fraction of repos — few enough to read
each one. It runs in shadow mode: tags appear on the dashboard and **no score
changes** until there is enough history to decide what it is worth.

## Worked example

`python examples/offline_demo.py` builds a repository with an invented history
and scores it:

```
  SCORE   8 / 10
  buyer   3   firms:halborn,openzeppelin; remediation
  timing  5   audit-scoped-branch; age<180d; velocity-crunch

  incumbent (raw)        halborn x3
  incumbent (filtered)   halborn x3   [1 dependency line(s) excluded]

  staging tag   staging
  new contract files 6 in 1 new folder(s), arrival 100%, novelty 86%
```

Reading it:

- **Buyer 3** — two firms are named (+2, counted once) and there is
  remediation language (+1). One of the two firm matches is a package install;
  it still earns buyer points, because the dependency filter applies to the
  incumbent flag only.
- **Timing 5** — a branch ending in `-audit` (+3), a repo 90 days old (+1), and
  six commits in a week against a baseline of zero (+1).
- **Incumbent** — one firm appears three times in fix commits. The package
  install is dropped and does not create a second incumbent.
- **Staging** — six new contract files landed in a new `src/v2/` folder inside
  the window.

## Known limits

- **It reads names, not code.** A team that never mentions its auditor and
  works on `main` is invisible.
- **Rules are hand-set.** The weights encode judgment from a small number of
  observed cases, not a fitted model. There is no measured precision or recall.
- **Keyword lists decay.** New firms appear; words acquire new meanings.
- **The 1–10 scale is coarse**, and many repos tie.
- **Public activity only.** Teams that develop in private and publish at launch
  show up late or not at all.
