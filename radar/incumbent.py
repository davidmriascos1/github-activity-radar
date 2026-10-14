"""
incumbent.py — dependency-aware incumbent detection.

The problem this solves: OpenZeppelin is simultaneously the most-used Solidity
library and an audit firm. `forge install: openzeppelin-contracts-upgradeable`
and `fix/oz-fee-order` both match the firm keyword, but they mean opposite
things — one is a package manager, the other is an auditor's finding being
remediated.

The cost is asymmetric. An inflated incumbent flag on a real prospect reads as
"they already have an auditor, skip them", which suppresses a lead. That is far
more expensive than a missed incumbent, which only costs a wasted conversation.
The case that motivated this: a repo with no auditor at all was flagged
`openzeppelin x5` off five package installs.

Scope is deliberately narrow: this reweighs the INCUMBENT COUNT ONLY. Buyer and
timing scoring are untouched, so no score moves. score.py is not modified — the
firm patterns and the counting rule are imported from it and applied to a
filtered view of the same commits.
"""
import re

import score

# Lines that are a package manager talking, not an auditor.
# Kept explicit rather than clever: every pattern here should be recognisable
# as tooling output or a dependency chore, never as a human describing an audit.
DEPENDENCY_PATTERNS = [
    r"forge\s+(install|update|remove)",
    r"soldeer\s+(install|update|push)",
    r"npm\s+(install|i|ci|add|update)\b",
    r"\byarn\s+(add|install|upgrade)\b",
    r"\bpnpm\s+(add|install|update)\b",
    r"\bbun\s+(add|install)\b",
    r"\bpip\s+install\b",
    r"\bcargo\s+(add|update)\b",
    r"\bgo\s+get\b",
    r"git\s+submodule",
    r"\.gitmodules\b",
    r"(package|yarn|pnpm|bun|cargo|gemfile|poetry|composer)[-.]?lock(\.json|\.yaml|\.toml)?\b",
    r"\bnode_modules\b",
    r"^\s*(chore|build|deps|ci)\s*\(\s*deps(-dev)?\s*\)",   # conventional-commit deps
    r"\b(bump|upgrade|update|add|remove|install|pin)\b[^\n]{0,40}\b"
    r"(dependency|dependencies|deps|submodule|submodules|package|packages)\b",
    r"\b(dependency|dependencies|deps|submodule|submodules)\b[^\n]{0,20}\b"
    r"(bump|upgrade|update|added|removed|installed|pinned)\b",
    r"\blib/[a-z0-9._-]+\b",                                 # foundry lib/ paths
]

_DEP_RE = [re.compile(p, re.I) for p in DEPENDENCY_PATTERNS]


def is_dependency_line(text):
    """True if this commit subject / branch name is package-management noise."""
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(p.search(t) for p in _DEP_RE)


def filter_dependency_lines(texts):
    """-> (kept, dropped)"""
    kept, dropped = [], []
    for t in texts:
        (dropped if is_dependency_line(t) else kept).append(t)
    return kept, dropped


def incumbent_from(subs, branches):
    """Re-run score.incumbent_flag over dependency-filtered text.

    Returns (flag, dropped) where dropped is the list of excluded lines, so the
    decision is auditable rather than a silent downgrade.
    """
    kept_subs, dropped_subs = filter_dependency_lines(subs or [])
    kept_branches, dropped_branches = filter_dependency_lines(branches or [])
    flag = score.incumbent_flag(kept_subs, kept_branches)
    return flag, dropped_subs + dropped_branches


def recompute(path):
    """Load a cloned repo and return (flag, dropped). Uses score.load, so the
    bot denylist and parsing rules stay identical to the scorer's."""
    commits, branches, _authors, _first, _last = score.load(path)
    return incumbent_from([s for _, s in commits], branches)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        flag, dropped = recompute(p)
        print(f"{p}\n  incumbent: {flag or '(none)'}  ({len(dropped)} dependency lines excluded)")
        for d in dropped[:6]:
            print(f"    - {d[:88]}")
