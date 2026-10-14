# GitHub Radar

**Reading public GitHub activity for early signs that a smart-contract team is
about to buy a security audit.**

A revenue-intelligence experiment: a daily scan that turns branch names, commit
history and code composition into a ranked, explained shortlist — and alerts
when something changes.

> Personal experimentation project. Not an official product of, or endorsed
> by, any employer or company.

```
  SCORE   8 / 10
  buyer   3   firms:halborn,openzeppelin; remediation
  timing  5   audit-scoped-branch; age<180d; velocity-crunch
  incumbent  halborn x3        staging  6 new contract files in src/v2/
```
<sub>Output for a synthetic repository — `python examples/offline_demo.py`.</sub>

## The problem

Smart-contract audits are bought in a narrow window: after the code is mostly
written, before it ships. Reach a team in that window and the conversation is
welcome. Reach them a month later and they have already signed with someone
else.

The usual way to find those teams is manual: watch launch announcements, scroll
social feeds, wait for an inbound request. All of these surface a team *after*
it has started shopping — often after it has decided.

But teams leave traces long before they announce anything. They open a branch
called `pre-audit`. They name an auditor in a commit message. They move a
finished version of the code into a fresh folder so it can be scoped and
quoted. They commit in a crunch. Every one of those is public, and none of them
needs anyone's permission to read.

## The idea

Treat those traces as **buying signals**, and read them systematically:

1. **Find** repositories with recent public activity.
2. **Filter** to the ones that are actually smart-contract code.
3. **Score** each on two questions — *is this team an audit buyer?* and *is the
   moment now?*
4. **Watch for change.** A score is a snapshot; an alert is a change. The radar
   compares every scan with the previous one and fires only when something
   crosses a threshold.
5. **Explain everything.** Every score is printed with the evidence that
   produced it. A number nobody can explain does not get acted on.

## How it works

```
public GitHub activity
  → discovery           repos with recent public events
  → language gate       what share of the code is a contract language?   langgate.py
  → profile             blobless clone: all branches, full history       profile.py
  → signals             buyer + timing score, incumbent, scope staging   score.py · incumbent.py · staging.py
  → change detection    what crossed a threshold since the last scan     alert.py
  → persistence         SQLite, keyed on GitHub's numeric repo id        store.py
  → views               contributor graph · HTML triage dashboard        graph.py · dashboard.py
```

Standard library only. No web framework, no ORM, no HTTP client dependency.

More detail: **[architecture](docs/architecture.md)** ·
**[scoring](docs/scoring.md)** · **[V1 retrospective](docs/v1-retrospective.md)**

## From V1 to V2

This repository keeps both versions, because the difference between them is
most of what the project taught.

**V1** ([`legacy/v1/`](legacy/v1/)) used a Google Sheet as database and
interface, sampled five branches and thirty days of commits per repo through the
GitHub API, and scored commit text against a lift-weighted keyword taxonomy. It
proved the signals were real.

**V2** ([`radar/`](radar/)) is a rewrite around the decision the output has to
support:

| | V1 | V2 |
|---|---|---|
| Data | API sample: 5 branches, 30 days | Full history from a blobless clone |
| Store | Google Sheet | SQLite, keyed on numeric repo id |
| Score | Lift-weighted keywords; leading vs lagging | Readable rules; buyer vs timing |
| Output | Rows in a sheet | Change alerts, dashboard, contributor graph |
| Dependencies | 4 packages + cloud service account | Standard library |
| New signals | Straight into the score | Shadow mode first |

The [retrospective](docs/v1-retrospective.md) covers what V1 got right, where it
ran out, and what V2 gave up in exchange.

## Try it

Python 3.9+ and `git`. Nothing to install for the radar itself.

**Offline, with synthetic data — no token, no network:**

```bash
python examples/offline_demo.py      # build an invented repo and score it
python scripts/score_report.py       # a strong and a weak repo, side by side
```

**The full daily scan, on a sample list of public libraries:**

```bash
cp .env.example .env                 # optional: add a GitHub token for a higher rate limit
python radar/run.py examples/repos.sample.txt
open data/out/dashboard.html
```

The sample list is well-known open-source tooling, chosen precisely because it
is *not* a list of prospects; expect low scores. See
[`examples/`](examples/) for what each sample file is.

**Tests:**

```bash
pip install pytest
pytest
```

The suite runs offline. It pins the scorer's current behaviour — keyword
matching, the buyer and timing rules and their caps, the velocity and age
boundaries, incumbent detection with and without the dependency filter — and
exercises the git parsing end to end against throwaway repositories built on
the fly.

## Repository layout

```
radar/        V2 — the current implementation
tests/        offline tests on synthetic input
examples/     synthetic and sample data, offline demo
scripts/      score report, bulk clone helper
docs/         architecture, scoring, V1 retrospective
legacy/v1/    the first implementation, archived
data/         runtime data — gitignored, empty in the repository
```

## Privacy and data hygiene

This project processes data about real people, so the rules are explicit.

- **What it reads is public**: repository metadata, branch names, commit
  subjects, file lists, and commit author addresses as they appear in public
  git history.
- **What it stores stays local.** Every runtime artefact — repo lists,
  discovery exports, the database, the contributor graph, the dashboard —
  resolves under `data/` ([`radar/paths.py`](radar/paths.py)), and `data/` is
  gitignored. Code and data never share a folder.
- **Nothing private is committed.** No target lists, no scan results, no
  contact data, no credentials, no internal identifiers. The only datasets in
  the repository are the synthetic ones in [`examples/`](examples/).
- **Credentials come from the environment.** See
  [`.env.example`](.env.example); `.env` is gitignored.
- **Bots are excluded** from the people the contributor graph tracks.

If you run it, you are responsible for how you use what it collects. Commit
author addresses are personal data in many jurisdictions; aggregating them for
outreach can carry legal obligations where you operate.

## Limitations

- **It reads names, not code.** A team that never mentions its auditor and works
  only on `main` is invisible.
- **The rules are judgment, not a fitted model.** They come from a small number
  of observed cases; there is no measured precision or recall.
- **Discovery is manual.** The candidate list is a periodic export prepared
  outside this repository.
- **Single user.** SQLite and a static HTML page; no shared state.
- **Public activity only.** Teams that build in private show up late or not at
  all.

## Future work

- **Measure it.** Record outcomes against scores and compute precision at the
  top of the list — then decide whether the rules should become learned weights.
- **Graduate scope staging out of shadow mode** once it has enough history.
- **Automate discovery** so the radar sources its own candidates.
- **More ecosystems.** The language gate already covers Solidity, Vyper, Cairo,
  Move, Noir, FunC and Michelson; the keyword and branch rules are tuned on EVM
  projects.
- **A shared view**, for when more than one person works the list.

## License

Copyright © 2026 David Riascos.

GitHub Radar is currently released under the
**[GNU Affero General Public License v3.0](LICENSE)** (`AGPL-3.0-only`).
You may use, modify and redistribute it under those terms; if you run a
modified version as a network service, the AGPL requires you to offer its
source to that service's users. See [`LICENSE`](LICENSE) for the full text.
