"""
GitHub Radar — keyword taxonomy (single source of truth).

Each entry: keyword (matched case-insensitively as a substring), its lift,
and its bucket. Buckets drive scoring:
    leading      → contributes to leading_score
    lagging      → contributes to lagging_score
    descriptive  → ecosystem_markers only, NO score (none for Solidity)

CRITICAL: these lift values are POOL-RELATIVE — valid only for the
Solidity/EVM pool they were computed in. When we add Cairo/Rust/ZK in
Phase 3, lift must be RE-COMPUTED inside each ecosystem's own pool, and
ranking/thresholding must stay WITHIN one ecosystem. Never compare scores
across ecosystems.

Grouped as Tier A (universal) and Tier B (Solidity/EVM). Candidate terms
with no validated lift (e.g. `mainnet deploy`, `reentrancy`) are intentionally
omitted — no points until lift is computed.
"""

SOLIDITY_KEYWORDS = [
    # ─── Tier A — Universal ───────────────────────────────────────────────
    {"keyword": "tag v",           "lift": 3.79, "bucket": "leading"},
    {"keyword": "fuzz test",       "lift": 3.22, "bucket": "leading"},
    {"keyword": "finalize",        "lift": 3.17, "bucket": "leading"},
    {"keyword": "invariant",       "lift": 3.05, "bucket": "leading"},
    {"keyword": "security.md",     "lift": 2.71, "bucket": "leading"},
    # these three shared a range (2.72–2.77); values split across the band
    {"keyword": "overflow",        "lift": 2.72, "bucket": "leading"},
    {"keyword": "underflow",       "lift": 2.77, "bucket": "leading"},
    {"keyword": "exploit",         "lift": 2.77, "bucket": "leading"},
    # "mid, leans lagging" → lagging per the agreed rule
    {"keyword": "finding",         "lift": 3.10, "bucket": "lagging"},
    {"keyword": "audit findings",  "lift": 3.90, "bucket": "lagging"},
    {"keyword": "security review", "lift": 3.79, "bucket": "lagging"},
    {"keyword": "cve",             "lift": 2.39, "bucket": "lagging"},
    {"keyword": "vulnerability",   "lift": 2.42, "bucket": "lagging"},

    # ─── Tier B — Solidity / EVM specific ─────────────────────────────────
    {"keyword": "storage layout",  "lift": 3.74, "bucket": "leading"},
    {"keyword": "oracle update",   "lift": 3.59, "bucket": "leading"},
]


# ─── Branch-name signals (NO validated lift) ──────────────────────────────────
# These are matched against normalized branch names only (separators like
# -, /, _ turned into spaces first). They are DELIBERATELY kept OUT of the
# pure-lift leading_score / lagging_score — we have no lift for them, and we
# refuse to invent lift values. Instead they are reported in their own
# "branch signal" dimension, so a branch literally named `audit-prep` is
# surfaced loudly without polluting the lift-based numbers. If real lift is
# computed later, they graduate into the main taxonomy above.
#
# Bucketing (decided): audit-prep / pre-audit → LEADING (getting ready);
# bare `audit` / post-audit / audit-fixes → LAGGING (underway or done).

# Phrases (normalized, space-separated) that mark a branch as audit PREP.
# Checked BEFORE bare "audit" so prep wins the precedence.
AUDIT_PREP_PHRASES = [
    "audit prep", "pre audit", "preaudit", "prep audit",
    "prepare audit", "prepare for audit", "preparing audit",
    "audit ready", "ready for audit", "pre audit prep",
]

# Non-audit leading branch signals: (label, [normalized phrases]).
BRANCH_LEADING_SIGNALS = [
    ("mainnet-deploy",    ["mainnet deploy"]),
    ("release-candidate", ["release candidate"]),
]

# Explicit lagging audit phrases (post/remediation). Bare "audit" also → lagging
# by default (handled in the scorer's precedence logic).
AUDIT_LAGGING_PHRASES = [
    "post audit", "audit fix", "audit fixes", "audit response",
    "audit remediation", "remediation",
]
