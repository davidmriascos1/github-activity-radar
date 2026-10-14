"""
score_report.py — the scorer's output as a readable table.

    python scripts/score_report.py                    # two synthetic repos, no network
    python scripts/score_report.py PATH [PATH ...]    # local clones of your own

With no arguments it builds two invented repositories in a temp directory (one
heading into an audit, one mature and quiet) and scores both, so you can see
what a strong and a weak result look like side by side. With arguments it
scores the local git clones you point it at; the label is the folder name.

Prints, per repo: total / buyer / timing, the incumbent flag, and the evidence
that fired. Then the contributor overlap between every pair of repos — the
"shared human" edge the contributor graph is built from.

Stdlib only. Requires git on PATH.
"""
import itertools
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "radar"))
sys.path.insert(0, os.path.join(_ROOT, "examples"))

import score  # noqa: E402


def synthetic_repos(workdir):
    import offline_demo
    return [
        ("demo/synthetic-vault (audit-bound)", offline_demo.build_demo_repo(workdir)),
        ("demo/quiet-library (no signal)", offline_demo.build_quiet_repo(workdir)),
    ]


def report(repos):
    profs = {}
    print(f"{'repo':38} {'SCORE':>5} {'buy':>4} {'tim':>4}  {'incumbent':<14} branches/commits")
    print("-" * 100)
    for label, path in repos:
        if not os.path.isdir(path):
            print(f"{label:38} {'SKIP':>5}  (not a directory: {path})")
            continue
        p = score.profile(path, label)
        profs[label] = p
        print(f"{label:38} {p['score']:>5} {p['buyer']:>4} {p['timing']:>4}  "
              f"{p['incumbent'] or '—':<14} {p['n_branches']}/{p['n_commits']}")
        print(f"{'':38}   buyer:  {'; '.join(p['buyer_hits']) or '(none)'}")
        print(f"{'':38}   timing: {'; '.join(p['timing_hits']) or '(none)'}")
        print()

    print("=" * 100)
    print("CONTRIBUTOR OVERLAP")
    print("=" * 100)
    found = False
    for a, b in itertools.combinations(profs, 2):
        common = profs[a]["authors"] & profs[b]["authors"]
        if common:
            found = True
            print(f"  {a.split(' (')[0]:24} <-> {b.split(' (')[0]:24} "
                  f"{len(common)} shared  {sorted(common)[:2]}")
    if not found:
        print("  (none)")
    return profs


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if shutil.which("git") is None:
        raise SystemExit("git is required")

    if argv:
        report([(os.path.basename(os.path.normpath(p)), p) for p in argv])
        return 0

    workdir = tempfile.mkdtemp(prefix="radar-report-")
    try:
        report(synthetic_repos(workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
