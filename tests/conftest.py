"""Shared pytest setup: import paths and the two synthetic git repositories.

The modules under radar/ import each other by bare name (`import score`), the
way they do when run as scripts, so that directory goes on sys.path. The
synthetic repo builders live in examples/offline_demo.py.
"""
import os
import shutil
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("radar", "examples"):
    path = os.path.join(_ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture(scope="session")
def audit_repo(tmp_path_factory):
    """Clone of a synthetic repo whose history looks like a team going into an
    audit: scoped branch, firm-named fixes, a burst of commits, a new version
    folder. Entirely invented."""
    import offline_demo
    return offline_demo.build_demo_repo(str(tmp_path_factory.mktemp("audit")))


@pytest.fixture(scope="session")
def quiet_repo(tmp_path_factory):
    """Clone of a synthetic mature library with no audit signal."""
    import offline_demo
    return offline_demo.build_quiet_repo(str(tmp_path_factory.mktemp("quiet")))
