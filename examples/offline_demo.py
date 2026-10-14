"""
offline_demo.py — run the scoring core on a synthetic repo. No network, no token.

    python examples/offline_demo.py

Builds a throwaway git repository in a temp directory whose history is written
to look like a team heading into an audit, clones it (the scorer reads remote
branches, exactly as it does on a real clone), then runs the three analysers
the daily scan runs on every repo:

    score.profile            buyer + timing score, with the evidence that fired
    incumbent.recompute      incumbent auditor, with package-manager noise removed
    staging.staging          the scope-staging footprint (shadow-mode signal)

Everything here is invented: the project, the people, the commit messages.
The audit-firm names are real public firms because recognising them is what
the scorer does.

Stdlib only. Requires git on PATH.
"""
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "radar"))

import incumbent  # noqa: E402
import score      # noqa: E402
import staging    # noqa: E402

AUTHORS = [
    ("Alice Example", "alice@example.com"),
    ("Bob Example", "bob@example.org"),
    ("Carol Example", "carol@example.net"),
    ("Dana Example", "dana@example.com"),      # only ever commits to the quiet repo
]


def _git(cwd, *args, when=None, author=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    name, email = author or AUTHORS[0]
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    if when is not None:
        stamp = f"{when.isoformat()}T12:00:00"
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True)


def _commit(repo, message, files, when, author=0):
    """Write `files` ({relative path: content}) and commit them."""
    for rel, content in files.items():
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, when=when, author=AUTHORS[author])


def build_demo_repo(workdir, today=None):
    """Create origin + clone under `workdir`. Returns the path of the clone."""
    today = today or dt.date.today()
    ago = lambda days: today - dt.timedelta(days=days)  # noqa: E731

    origin = os.path.join(workdir, "origin")
    os.makedirs(origin)
    _git(origin, "init", "-b", "main")

    # Three months ago: the project starts with a single contract.
    _commit(origin, "feat: initial vault contract",
            {"src/Vault.sol": "// v1\n", "README.md": "demo\n"}, ago(90))
    _commit(origin, "forge install: openzeppelin-contracts",
            {"lib/README.md": "vendored\n"}, ago(88), author=1)
    _commit(origin, "feat: fee accounting",
            {"src/Vault.sol": "// v1 fees\n"}, ago(60), author=1)

    # Last week: a new, finished version lands in its own folder, in a burst.
    # This is the scope-staging footprint.
    v2 = {f"src/v2/{name}.sol": f"// {name}\n" for name in
          ("Vault", "Strategy", "Oracle", "Router", "Fees", "Access")}
    _commit(origin, "feat: v2 contracts", v2, ago(6), author=2)

    # ...followed by an auditor's findings being worked through.
    fixes = [
        "fix: halborn finding H-01 reentrancy in withdraw",
        "fix: halborn finding M-02 oracle staleness check",
        "fix: halborn finding L-03 event emission",
        "chore: audit fixes round one",
        "test: invariant tests for vault accounting",
        "docs: update deployment notes",
    ]
    # (The demo edits a test file so the six new contracts stay untouched:
    # staging measures arrival of new contract files, not rework of them.)
    for i, message in enumerate(fixes):
        _commit(origin, message, {"test/Vault.t.sol": f"// rev {i}\n"},
                ago(4), author=i % 3)

    # A branch whose name says a frozen scope exists.
    _git(origin, "branch", "release/v2.0-audit")

    clone = os.path.join(workdir, "clone")
    _git(workdir, "clone", origin, clone)
    return clone


def build_quiet_repo(workdir, today=None):
    """A mature, slow-moving library with no audit signal at all — the contrast
    case. Shares one contributor (Alice) with the demo repo above, so the
    contributor-overlap view has an edge to show. Returns the clone path."""
    today = today or dt.date.today()
    ago = lambda days: today - dt.timedelta(days=days)  # noqa: E731

    origin = os.path.join(workdir, "quiet-origin")
    os.makedirs(origin)
    _git(origin, "init", "-b", "main")

    history = [
        (400, "feat: initial math library", 0),
        (300, "feat: add fixed-point helpers", 0),
        (200, "docs: usage examples", 3),
        (150, "fix: rounding in mulDiv", 0),
        (100, "chore: bump compiler version", 3),
        (60, "test: more fuzz cases", 0),
        (30, "docs: changelog", 3),
        (10, "fix: typo in natspec", 0),
    ]
    for i, (days, message, author) in enumerate(history):
        _commit(origin, message, {"src/Math.sol": f"// rev {i}\n"},
                ago(days), author=author)

    clone = os.path.join(workdir, "quiet-clone")
    _git(workdir, "clone", origin, clone)
    return clone


def main():
    if shutil.which("git") is None:
        raise SystemExit("git is required for the offline demo")

    workdir = tempfile.mkdtemp(prefix="radar-demo-")
    try:
        clone = build_demo_repo(workdir)

        prof = score.profile(clone, "demo/synthetic-vault")
        flag, dropped = incumbent.recompute(clone)
        stage = staging.staging(clone)

        print("synthetic repo: demo/synthetic-vault  (invented history)\n")
        print(f"  SCORE   {prof['score']} / 10")
        print(f"  buyer   {prof['buyer']}   {'; '.join(prof['buyer_hits']) or '(nothing fired)'}")
        print(f"  timing  {prof['timing']}   {'; '.join(prof['timing_hits']) or '(nothing fired)'}")
        print(f"  commits / branches / authors   "
              f"{prof['n_commits']} / {prof['n_branches']} / {prof['n_authors']}")
        print()
        print(f"  incumbent (raw)        {prof['incumbent'] or '(none)'}")
        print(f"  incumbent (filtered)   {flag or '(none)'}   "
              f"[{len(dropped)} dependency line(s) excluded]")
        print()
        print(f"  staging tag   {stage['tag'] or '(none)'}")
        print(f"  new contract files {stage['new_files']} in {stage['new_dirs']} new folder(s), "
              f"arrival {stage['arrival']:.0%}, novelty {stage['novelty']:.0%}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
