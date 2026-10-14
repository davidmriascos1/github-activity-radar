"""
paths.py — where runtime data lives.

Everything the radar reads or writes at runtime (the repo list, the SQLite
database, the Obsidian vault, the dashboard, the run history) sits under one
directory, and that directory is gitignored. Code and data never share a
folder, so a careless `git add .` cannot publish a scan.

Default is ./data at the repository root. RADAR_DATA_DIR moves it.

Stdlib only.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_HERE)

DATA_DIR = os.environ.get("RADAR_DATA_DIR") or os.path.join(REPO_ROOT, "data")


def data_path(*parts):
    """Join `parts` under the runtime data directory."""
    return os.path.join(DATA_DIR, *parts)
