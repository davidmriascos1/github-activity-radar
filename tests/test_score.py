"""
Tests for the scoring core (radar/score.py) and the incumbent filter built on it.

They pin the scorer's CURRENT behaviour — constants, weights and caps are read
from score.py, not redefined here. Every branch name, commit subject, person
and project below is invented. Nothing touches the network: the git-backed
tests run against throwaway repositories built by examples/offline_demo.py.
"""
import datetime
import os

import pytest

import incumbent
import score
from conftest import needs_git

TODAY = datetime.date(2026, 1, 15)


def days_ago(n):
    return TODAY - datetime.timedelta(days=n)


def weekly(per_week, weeks, anchor=TODAY):
    """`per_week` commits in each of the `weeks` weeks ending at `anchor`."""
    return [(anchor - datetime.timedelta(days=7 * w), "chore: work")
            for w in range(weeks) for _ in range(per_week)]


# --------------------------------------------------------------------------
# keyword matching
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["pre-audit", "pre audit", "pre_audit", "preaudit"])
def test_hyphen_in_keyword_matches_space_underscore_or_nothing(text):
    assert score.kw("pre-audit").search(text)


def test_keyword_needs_word_boundaries():
    assert score.kw("oz").search("fix/oz-fee-order")
    assert not score.kw("oz").search("frozen balances")


def test_strict_keyword_requires_the_literal_hyphen():
    # "ai review" with a space is how people talk about AI code-review bots,
    # not audits — score.STRICT_KW exists to keep those apart.
    assert "ai-review" in score.STRICT_KW
    assert score.kw("ai-review").search("run ai-review before merge")
    assert not score.kw("ai-review").search("address ai review comments")


# --------------------------------------------------------------------------
# audit-scoped branches
# --------------------------------------------------------------------------

@pytest.mark.parametrize("branch", [
    "origin/pre-audit",
    "origin/refactor/pre-audit",
    "origin/feat/v0.6.0-audit",       # structural: name ends in -audit
    "origin/code-freeze",
    "origin/audit-scope",
])
def test_scoped_branch_is_detected(branch):
    assert score.has_scoped_branch([branch])


@pytest.mark.parametrize("branch", ["audit", "origin/audit"])
def test_bare_audit_branch_is_ignored_as_wallpaper(branch):
    assert not score.has_scoped_branch([branch])


def test_ordinary_branches_are_not_scoped():
    assert not score.has_scoped_branch(["origin/main", "origin/feat/fees"])
    assert not score.has_scoped_branch([])


# --------------------------------------------------------------------------
# buyer score
# --------------------------------------------------------------------------

def test_firm_named_in_a_branch():
    assert score.score_buyer(["origin/fix/halborn-h-01"], []) == (2, ["firms:halborn"])


def test_firm_named_only_in_commit_subjects_still_counts():
    # BUYER = branches UNION commits.
    assert score.score_buyer(["origin/main"], ["fix: certora rule violation"]) \
        == (2, ["firms:certora"])


def test_several_firms_are_reported_sorted_but_score_once():
    pts, hits = score.score_buyer([], ["zellic review", "cantina contest prep"])
    assert (pts, hits) == (2, ["firms:cantina,zellic"])


def test_ai_audit_is_worth_two():
    assert score.score_buyer([], ["chore: run ai-audit"]) == (2, ["ai-audit"])


def test_remediation_alone_is_worth_one():
    assert score.score_buyer([], ["post-audit cleanup"]) == (1, ["remediation"])


def test_all_three_buyer_signals_reach_the_cap_of_five():
    pts, hits = score.score_buyer(
        ["origin/spearbit-fixes"],
        ["chore: run ai-audit", "fix: audit fixes applied"],
    )
    assert pts == 5
    assert hits == ["firms:spearbit", "ai-audit", "remediation"]


def test_nothing_fires_on_ordinary_history():
    assert score.score_buyer(["origin/main"], ["feat: add fees"]) == (0, [])
    assert score.score_buyer([], []) == (0, [])


# --------------------------------------------------------------------------
# timing score
# --------------------------------------------------------------------------

def test_scoped_branch_is_worth_three():
    assert score.score_timing(["origin/pre-audit"], [], days_ago(400), today=TODAY) \
        == (3, ["audit-scoped-branch"])


def test_age_rule_boundary_is_strictly_under_180_days():
    assert score.score_timing([], [], days_ago(179), today=TODAY) == (1, ["age<180d"])
    assert score.score_timing([], [], days_ago(180), today=TODAY) == (0, [])


def test_age_rule_uses_the_scan_date_it_is_given():
    first = datetime.date(2025, 12, 1)
    assert score.score_timing([], [], first, today=datetime.date(2026, 1, 1))[0] == 1
    assert score.score_timing([], [], first, today=datetime.date(2026, 12, 1))[0] == 0


def test_unknown_first_commit_does_not_crash_or_score():
    assert score.score_timing([], [], None, today=TODAY) == (0, [])


def test_all_three_timing_signals_reach_the_cap_of_five():
    pts, hits = score.score_timing(
        ["origin/pre-audit"], weekly(5, 1), days_ago(30), today=TODAY)
    assert pts == 5
    assert hits == ["audit-scoped-branch", "age<180d", "velocity-crunch"]


def test_stronger_timing_evidence_scores_higher():
    quiet = score.score_timing(["origin/main"], [], days_ago(400), today=TODAY)[0]
    young = score.score_timing(["origin/main"], [], days_ago(30), today=TODAY)[0]
    scoped = score.score_timing(["origin/pre-audit"], [], days_ago(30), today=TODAY)[0]
    assert quiet < young < scoped


# --------------------------------------------------------------------------
# velocity
# --------------------------------------------------------------------------

def test_no_commits_is_not_a_spike():
    assert score.velocity_spike([]) is False


def test_steady_cadence_is_not_a_spike():
    assert not score.velocity_spike(weekly(2, 27))


def test_burst_from_cold_needs_five_commits_in_a_week():
    assert not score.velocity_spike(weekly(4, 1))
    assert score.velocity_spike(weekly(5, 1))


def test_spike_must_exceed_twice_the_baseline_median():
    steady = weekly(2, 27)                       # baseline median = 2 per week
    assert not score.velocity_spike(steady + weekly(2, 1))   # week of 4: not > 4
    assert score.velocity_spike(steady + weekly(3, 1))       # week of 5: > 4


# --------------------------------------------------------------------------
# incumbent
# --------------------------------------------------------------------------

def test_incumbent_needs_three_mentions():
    two = [f"fix: halborn finding {i}" for i in range(2)]
    three = [f"fix: halborn finding {i}" for i in range(3)]
    assert score.incumbent_flag(two, []) == ""
    assert score.incumbent_flag(three, []) == "halborn x3"


def test_incumbent_counts_branches_and_commits_together():
    assert score.incumbent_flag(
        ["fix: zellic 1", "fix: zellic 2"], ["origin/zellic-fixes"]) == "zellic x3"


def test_most_mentioned_firm_wins():
    subs = ["halborn a", "halborn b", "halborn c",
            "zellic a", "zellic b", "zellic c", "zellic d"]
    assert score.incumbent_flag(subs, []) == "zellic x4"


def test_package_installs_are_not_an_incumbent():
    subs = ["forge install: openzeppelin-contracts"] * 5
    # The raw rule cannot tell a library install from an auditor...
    assert score.incumbent_flag(subs, []) == "openzeppelin x5"
    # ...which is exactly what incumbent.py filters out.
    flag, dropped = incumbent.incumbent_from(subs, [])
    assert flag == ""
    assert len(dropped) == 5


def test_real_remediation_survives_the_dependency_filter():
    subs = [f"fix: openzeppelin audit finding L-0{i}" for i in range(3)]
    assert incumbent.incumbent_from(subs, []) == ("openzeppelin x3", [])


def test_incumbent_filter_handles_empty_input():
    assert incumbent.incumbent_from([], []) == ("", [])
    assert incumbent.incumbent_from(None, None) == ("", [])


# --------------------------------------------------------------------------
# end to end, on synthetic git repositories
# --------------------------------------------------------------------------

@needs_git
def test_profile_of_an_audit_bound_repo(audit_repo):
    prof = score.profile(audit_repo, "demo/synthetic-vault")
    assert prof["buyer"] == 3
    assert prof["buyer_hits"] == ["firms:halborn,openzeppelin", "remediation"]
    assert prof["timing"] == 5
    assert prof["timing_hits"] == ["audit-scoped-branch", "age<180d", "velocity-crunch"]
    assert prof["score"] == 8
    assert prof["incumbent"] == "halborn x3"
    assert (prof["n_commits"], prof["n_authors"]) == (10, 3)


@needs_git
def test_profile_of_a_quiet_repo_floors_at_one(quiet_repo):
    prof = score.profile(quiet_repo, "demo/quiet-library")
    assert (prof["buyer"], prof["timing"]) == (0, 0)
    assert prof["buyer_hits"] == [] and prof["timing_hits"] == []
    assert prof["score"] == 1          # the total is clamped to 1..10
    assert prof["incumbent"] == ""


@needs_git
def test_strong_signals_outrank_weak_ones(audit_repo, quiet_repo):
    strong = score.profile(audit_repo, "strong")
    weak = score.profile(quiet_repo, "weak")
    assert strong["score"] > weak["score"]
    assert 1 <= weak["score"] <= strong["score"] <= 10


@needs_git
def test_shared_contributor_is_visible_across_repos(audit_repo, quiet_repo):
    a = score.profile(audit_repo, "a")["authors"]
    b = score.profile(quiet_repo, "b")["authors"]
    assert a & b == {"alice@example.com"}


@needs_git
def test_dependency_filter_on_a_real_clone(audit_repo):
    flag, dropped = incumbent.recompute(audit_repo)
    assert flag == "halborn x3"
    assert dropped == ["forge install: openzeppelin-contracts"]


@needs_git
def test_empty_repository_does_not_crash(tmp_path):
    import offline_demo
    offline_demo._git(str(tmp_path), "init", "-b", "main")
    prof = score.profile(str(tmp_path), "demo/empty")
    assert prof["n_commits"] == 0
    assert prof["first"] is None
    assert prof["score"] == 1


@needs_git
def test_load_drops_bots_and_normalises_noreply_addresses(tmp_path):
    import offline_demo
    repo = str(tmp_path)
    offline_demo._git(repo, "init", "-b", "main")
    people = [
        ("Bob Example", "bob@example.org"),
        ("Alice Example", "12345+alice@users.noreply.github.com"),
        ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"),
    ]
    for i, who in enumerate(people):
        with open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"{i}\n")
        offline_demo._git(repo, "add", "-A")
        offline_demo._git(repo, "commit", "-m", f"chore: change {i}", author=who)

    commits, _branches, authors, _first, _last = score.load(repo)
    assert len(commits) == 3                       # bots still count as activity
    assert authors == {"bob@example.org", "alice"}  # ...but not as people
