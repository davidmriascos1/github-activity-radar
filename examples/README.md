# Examples

Everything in this folder is synthetic or deliberately generic. It is the only
data committed to the repository; real inputs and outputs live in `data/`,
which is gitignored.

| File | What it is | Real or invented |
|---|---|---|
| `offline_demo.py` | Builds two throwaway git repos and runs the scoring core on one of them | Invented history, people and project |
| `repos.sample.txt` | Input for `radar/run.py`: one GitHub URL per line | Real public libraries and tooling, chosen because they are obviously not prospects |
| `bq-orgs.sample.csv` | Shape of a weekly discovery export: repos with public activity in a window | Invented names and numbers, real column layout |
| `langgate_results.sample.csv` | Shape of the language gate's output | Invented names and numbers, real column layout |

The org names in the two CSVs (`example-org`, `acme-labs`, `sample-dao`,
`demo-protocol`) are placeholders. If an account with one of those names
exists on GitHub, the rows here say nothing about it.

## Try it

**No network, no token** — score a synthetic repo:

```bash
python examples/offline_demo.py
python scripts/score_report.py
```

**With network** — run the full daily scan on the sample list. It clones six
public repos into a temp directory, scores them, and writes a database, a
contributor vault and a dashboard under `data/out/`:

```bash
cp .env.example .env        # add a GitHub token (optional, raises the rate limit)
python radar/run.py examples/repos.sample.txt
open data/out/dashboard.html
```

Expect low scores: these are mature libraries, not teams preparing an audit.
That is the point of the sample — it shows the mechanics without pointing at
anyone.

**Language gate only** — one API call per repo, no clones:

```bash
python radar/langgate.py examples/repos.sample.txt -o data/langgate_results.csv
```

## Column reference

`bq-orgs.sample.csv`

| Column | Meaning |
|---|---|
| `repo_name` | `owner/repo` |
| `event_count` | public events (pushes, PRs, branch creations…) seen in the window |
| `first_seen`, `last_seen` | first and last event timestamps in the window, UTC |

`langgate_results.sample.csv`

| Column | Meaning |
|---|---|
| `repo_url` | the URL that was checked |
| `contract_pct` | bytes of smart-contract languages ÷ total bytes, 0–1; blank if the lookup failed |
| `primary_lang` | language with the most bytes |
| `total_bytes` | total bytes GitHub attributes to any language |
| `status` | `ok`, `empty`, or the failure (`404`, `403:rate-limited`, `403:forbidden`, …) |
