# GitHub Radar V1 (archived)

The first implementation, kept for reference. **It is superseded by V2 in
[`radar/`](../../radar/)** and is not maintained. For what it taught and why it
was rewritten, see the [V1 retrospective](../../docs/v1-retrospective.md).

The code is as it was, with three changes made for publication:

- the Google Sheet ID is read from `RADAR_SHEET_ID` instead of being written in
  `config.py`
- the service-account key path can be set with `GOOGLE_SERVICE_ACCOUNT_FILE`
- the seed list in `seed_repos.py` is a set of placeholder public libraries

## What it does

Reads a list of Solidity repositories from a Google Sheet, samples each one's
recent branches and commits through the GitHub API, scores them against a
keyword taxonomy, and writes the result back to the sheet.

| File | Role |
|---|---|
| `config.py` | Sheet target, tab names and column schemas — the single source of truth |
| `keywords.py` | Keyword taxonomy with lift values; branch-name signals |
| `setup_sheet.py` | Creates the three tabs with their headers |
| `seed_repos.py` | Appends a few example repos to the source tab |
| `radar_filter.py` | Builds the source list: ecosystem roster → activity filter |
| `radar_cycle.py` | The scan: branches → commits → scores → sheet |
| `smoke_test.py` | Checks that the sheet and the GitHub token both work |

## Running it

V1 needs things V2 does not: a Google Cloud service account with access to a
sheet you own, and four third-party packages.

```bash
pip install gspread google-auth requests duckdb

cd legacy/v1
echo "GITHUB_TOKEN=your_github_token_here" > .env
export RADAR_SHEET_ID=your_google_sheet_id_here
export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account-key.json

python smoke_test.py      # sheet + token reachable?
python setup_sheet.py     # create the tabs
python seed_repos.py      # add example repos
python radar_cycle.py     # dry run; add --write to write to the sheet
```

`radar_filter.py` additionally expects a local DuckDB file of open-source
ecosystem data, built by a separate ingestion step that is not part of this
repository. Without it, seed the source tab by hand or with `seed_repos.py`.

The scripts use relative paths and must be run from this directory.
