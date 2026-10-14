"""
profile.py — the language gate.

Wraps the tested scorer in score.py. Does NOT modify or re-derive any scoring
logic: it clones a repo, hands the path to score.profile(), then enriches the
resulting dict with GitHub metadata (numeric repo id, created_at) and a
contract-language percentage.

contract_pct is RECORDED, NOT ENFORCED. No hard rejection yet — the point of
this step is to eyeball the borderline repos before setting a threshold.

Stdlib only: urllib for the API, subprocess for git.
"""
import json
import os
import pprint
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import incumbent as incumbent_mod
import score
import staging as staging_mod

# Languages that indicate on-chain code. Same set as langgate.py.
CONTRACT_LANGS = {"Solidity", "Vyper", "Cairo", "Move", "Noir", "FunC", "Michelson"}

GITHUB_API = "https://api.github.com"
UA = "github-radar-v2"

# Seconds a single blobless clone may run before we give up on it.
#
# Why 300 and not something tight: a blobless clone pulls refs and commit
# metadata but no file blobs, so it normally finishes in seconds — a few tens
# of seconds for a genuinely large repo. 300s is therefore not a performance
# budget, it is a deadlock breaker. The only legitimate thing that should ever
# approach it is a huge monorepo over a slow link, and cutting one of those off
# would lose a real lead. Everything else that hits this ceiling is stuck, not
# slow: a host that accepted the connection and then went quiet, a proxy
# holding the socket open, a half-dead mirror.
#
# The asymmetry is the whole argument. Waiting 300s on a repo that was going to
# finish anyway costs five minutes once. Having no ceiling at all costs the
# entire run, because one hung clone blocks every repo behind it — which is
# exactly the failure this constant exists to prevent.
CLONE_TIMEOUT = 300


# --------------------------------------------------------------------------
# env / token
# --------------------------------------------------------------------------

def _load_dotenv(path=None):
    """Populate GITHUB_TOKEN from a .env file if it isn't already in the env.

    Looks next to this file, then one directory up. Never overwrites a token
    that is already exported in the shell.
    """
    if os.environ.get("GITHUB_TOKEN"):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [path] if path else [
        os.path.join(here, ".env"),
        os.path.join(os.path.dirname(here), ".env"),
    ]
    for c in candidates:
        if not c or not os.path.isfile(c):
            continue
        try:
            with open(c) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == "GITHUB_TOKEN":
                        v = v.strip().strip('"').strip("'")
                        if v:
                            os.environ["GITHUB_TOKEN"] = v
                            return
        except OSError:
            continue


def _token():
    _load_dotenv()
    return os.environ.get("GITHUB_TOKEN", "")


# --------------------------------------------------------------------------
# url parsing
# --------------------------------------------------------------------------

_URL_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+?)(?:\.git)?/?$",
    re.I,
)


def parse_owner_repo(url):
    """'https://github.com/example-org/example-repo' -> ('example-org', 'example-repo')"""
    m = _URL_RE.search(url.strip())
    if not m:
        raise ValueError(f"cannot parse owner/repo from: {url}")
    return m.group("owner"), m.group("repo")


# --------------------------------------------------------------------------
# github api (urllib, no requests)
# --------------------------------------------------------------------------

# Longest we will ever sleep waiting for a primary-quota reset, as a guard
# against a bogus or far-future X-RateLimit-Reset header. Same as langgate.py.
RATE_MAX_SLEEP = 3900
# Longest we will honour a secondary limiter's Retry-After. Same as langgate.py.
SECONDARY_MAX_SLEEP = 300

# How many times one API call will sleep through a rate limit and retry before
# giving up on the repo.
RATE_MAX_RETRIES = 3

# (i, n) of the repo run.py is currently on, so the rate-limit pause line can
# say where the scan will resume. None when profile.py is used on its own.
PROGRESS = None


def _rate_limit_nap(e):
    """-> (seconds_to_sleep, label) for a 403/429 that is a genuine rate
    limit, or None when it carries no rate-limit evidence.

    Same classification as langgate.py. Not every 403 is a rate limit: a
    blocked/suspended account, a token without the right scope, or a proxy
    that gates which repos are reachable all answer 403 with quota left and no
    Retry-After. Sleeping on those burns the run waiting for a window that was
    never closed, so they must keep failing fast.
    """
    hdrs = e.headers or {}
    retry_after = hdrs.get("Retry-After")
    remaining = reset_at = None
    try:
        v = hdrs.get("X-RateLimit-Remaining")
        if v is not None:
            remaining = int(v)
    except (TypeError, ValueError):
        pass
    try:
        v = hdrs.get("X-RateLimit-Reset")
        if v is not None:
            reset_at = int(v)
    except (TypeError, ValueError):
        pass

    has_quota_hdrs = hdrs.get("X-RateLimit-Remaining") is not None
    exhausted = remaining is not None and remaining <= 0
    if not (exhausted or retry_after or (has_quota_hdrs and e.code == 429)):
        return None

    if retry_after and not exhausted:
        # Secondary/abuse limiter: it tells us how long to wait.
        try:
            nap = max(1, min(int(retry_after), SECONDARY_MAX_SLEEP))
        except ValueError:
            nap = 60
        return nap, "secondary limit"

    if not reset_at:
        nap = 60
    else:
        nap = max(0, min(int(reset_at - time.time()) + 3, RATE_MAX_SLEEP))
    return nap or 60, f"{0 if remaining is None else remaining} calls left"


def _gh_get_once(path, token=None, timeout=30):
    req = urllib.request.Request(GITHUB_API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", UA)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    tok = token if token is not None else _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def gh_get(path, token=None, timeout=30):
    """GET {GITHUB_API}{path} -> parsed JSON. Raises on transport/HTTP error.

    A 403/429 with rate-limit evidence sleeps until the window reopens and
    then retries the SAME request, up to RATE_MAX_RETRIES times, so the repo
    being profiled is scored rather than skipped. Any other 403 raises
    immediately, as before.
    """
    rate_retries = 0
    while True:
        try:
            return _gh_get_once(path, token=token, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429) or rate_retries >= RATE_MAX_RETRIES:
                raise
            limited = _rate_limit_nap(e)
            if limited is None:
                raise
            nap, label = limited
            rate_retries += 1
            when = time.strftime("%H:%M", time.localtime(time.time() + nap))
            where = (f", then resuming at repo {PROGRESS[0]}/{PROGRESS[1]}"
                     if PROGRESS else ", then retrying")
            print(f"[rate limit] retry {rate_retries}/{RATE_MAX_RETRIES} — "
                  f"{label} — sleeping {nap}s until {when}{where}", flush=True)
            time.sleep(nap)


def language_breakdown(owner, repo, token=None):
    """-> (contract_pct, primary_lang, total_bytes).

    contract_pct = bytes in CONTRACT_LANGS / total bytes, 0.0..1.0
    primary_lang = language with the most bytes, or None for an empty repo
    """
    langs = gh_get(f"/repos/{owner}/{repo}/languages", token=token)
    total = sum(langs.values())
    if not total:
        return 0.0, None, 0
    contract = sum(b for lang, b in langs.items() if lang in CONTRACT_LANGS)
    primary = max(langs.items(), key=lambda kv: kv[1])[0]
    return contract / total, primary, total


def repo_meta(owner, repo, token=None):
    """-> (numeric_id, full_name, created_at). We key on the numeric id, never
    on owner/name — repos get renamed and transferred."""
    d = gh_get(f"/repos/{owner}/{repo}", token=token)
    return d.get("id"), d.get("full_name"), d.get("created_at")


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------

# os.killpg + start_new_session are POSIX. True on macOS and Linux; on a
# platform without them we degrade to killing just the direct child, which is
# still better than hanging forever.
_CAN_KILL_GROUP = hasattr(os, "killpg") and hasattr(os, "getpgid")


def _kill_process_group(p, grace=5):
    """Kill a timed-out clone and every helper it spawned.

    This is the part subprocess.run(timeout=...) does NOT do. run() kills only
    the process it started, but `git clone` delegates the actual transfer to
    helper processes — git-remote-https holds the socket, git-index-pack writes
    the packfile. Kill the parent alone and those helpers are reparented to
    init and keep going: still connected, still writing into a directory
    profile_repo is about to delete. On a run over several hundred repos that is how you
    end up with stray git processes and a temp directory that refuses to stay
    deleted.

    Because the child was started with start_new_session, one signal to the
    group reaches all of them. SIGTERM first so git can unlink its partial
    packfile, SIGKILL only if it ignores that.
    """
    try:
        pgid = os.getpgid(p.pid) if _CAN_KILL_GROUP else None
    except OSError:
        pgid = None

    if pgid is None:
        # No process group available: kill the direct child and accept that a
        # helper may survive. Still better than hanging forever.
        try:
            p.kill()
        except OSError:
            pass
        try:
            p.communicate(timeout=grace)
        except Exception:
            pass
        return

    def _group_alive():
        """Signal 0 tests for delivery without delivering. Raises when the
        group has no members left."""
        try:
            os.killpg(pgid, 0)
            return True
        except OSError:
            return False

    # Escalate. Two things make this less obvious than it looks:
    #
    #  1. p.communicate() returning is NOT evidence the group is dead — it only
    #     means the direct child was reaped and its pipes closed. The helpers
    #     are precisely what this function is for, so the loop tests the group
    #     itself rather than trusting the parent's exit.
    #  2. A reaped-but-not-waited leader lingers as a zombie, and a zombie is
    #     still a group member, so the group never looks empty until we wait()
    #     on it. Hence the wait inside the poll.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            break  # group already empty

        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                p.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError):
                pass
            if not _group_alive():
                break
            time.sleep(0.1)

        if not _group_alive():
            break

    # Release the pipe fds. Everything that could still write to them has been
    # killed by now, so this cannot block.
    try:
        p.communicate(timeout=grace)
    except Exception:
        pass


def clone_blobless(url, dest, timeout=None):
    """Blobless, no-checkout clone. Fast: refs + commit metadata, no file blobs.
    git log --all and git branch -r both work against this.

    Bounded two ways, because a clone can stall for two different reasons:

    * `timeout` (default CLONE_TIMEOUT) caps a clone that is connected but not
      progressing. On expiry this raises RuntimeError, which run.py catches as
      one FAIL line before moving to the next repo.
    * GIT_TERMINAL_PROMPT=0 stops git asking for credentials. A private,
      renamed or deleted repo would otherwise sit on a username prompt that
      nobody is there to answer — an indefinite hang that no timeout should
      have to clean up in the first place.

    `timeout` is resolved from the module constant at call time rather than
    bound as a default argument, so tests can patch profile.CLONE_TIMEOUT and
    have it take effect.
    """
    timeout = CLONE_TIMEOUT if timeout is None else timeout
    cmd = ["git", "clone", "--filter=blob:none", "--no-checkout", url, dest]

    # Inherit the real environment (PATH, proxy vars, any git config the shell
    # exports) and override only the prompt behaviour.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")

    # start_new_session puts git and everything it spawns into one process
    # group. See _kill_process_group for why that matters on timeout.
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=_CAN_KILL_GROUP,
        )
    except OSError as e:
        raise RuntimeError(f"clone failed: cannot run git: {e}")

    try:
        _, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(p)
        raise RuntimeError(f"clone timed out after {timeout}s")

    if p.returncode != 0:
        # git prints "Cloning into '...'" before the real error. Keep the last
        # non-empty line so the FAIL line in run.py stays one scannable row.
        lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
        why = lines[-1] if lines else f"git exited {p.returncode}"
        raise RuntimeError(f"clone failed: {why[:200]}")
    return dest


# --------------------------------------------------------------------------
# the one public function
# --------------------------------------------------------------------------

def profile_repo(url, token=None, keep_clone=False):
    """Clone -> score -> enrich with GitHub metadata -> delete clone.

    Returns the score.profile() dict plus:
        repo_id, full_name, created_at, contract_pct, primary_lang,
        total_lang_bytes, url, api_error

    An API failure is recorded in api_error rather than raised, so a run over
    many repos is never killed by one rate-limited or 404'd lookup. A clone
    failure DOES raise — without a clone there is nothing to score.
    """
    owner, repo = parse_owner_repo(url)
    tmp = tempfile.mkdtemp(prefix="radar-")
    clone_dir = os.path.join(tmp, repo)
    try:
        clone_blobless(url, clone_dir)
        prof = score.profile(clone_dir, url)

        # Re-derive the incumbent flag with package-management lines excluded.
        # Display/flag only — score, buyer and timing are left exactly as
        # score.py computed them.
        flag, dropped = incumbent_mod.recompute(clone_dir)
        prof["incumbent_raw"] = prof.get("incumbent")
        prof["incumbent"] = flag
        prof["incumbent_dropped"] = len(dropped)
        # Step 4 (shadow mode): scope-staging check on the same clone, before it is deleted.
        prof["staging"] = staging_mod.safe_staging(clone_dir)
    finally:
        if not keep_clone:
            shutil.rmtree(tmp, ignore_errors=True)

    prof["url"] = url
    prof["full_name"] = f"{owner}/{repo}"
    prof["repo_id"] = None
    prof["created_at"] = None
    prof["contract_pct"] = None
    prof["primary_lang"] = None
    prof["total_lang_bytes"] = None
    prof["api_error"] = None

    try:
        rid, full_name, created_at = repo_meta(owner, repo, token=token)
        prof["repo_id"] = rid
        prof["full_name"] = full_name or prof["full_name"]
        prof["created_at"] = created_at
        pct, primary, total = language_breakdown(owner, repo, token=token)
        prof["contract_pct"] = round(pct, 4)
        prof["primary_lang"] = primary
        prof["total_lang_bytes"] = total
    except urllib.error.HTTPError as e:
        prof["api_error"] = f"HTTP {e.code} {e.reason}"
    except Exception as e:  # transport, DNS, proxy, malformed JSON
        prof["api_error"] = f"{type(e).__name__}: {e}"

    return prof


def _print(prof):
    p = dict(prof)
    p["authors"] = sorted(p.get("authors", set()))
    print(f"\n{p['full_name']}  (repo_id={p['repo_id']})")
    print(f"  SCORE {p['score']}   buyer {p['buyer']}   timing {p['timing']}")
    print(f"  buyer hits : {'; '.join(p['buyer_hits']) or '(none)'}")
    print(f"  timing hits: {'; '.join(p['timing_hits']) or '(none)'}")
    print(f"  incumbent  : {p['incumbent'] or '(none)'}")
    cp = p["contract_pct"]
    print(f"  contract_pct: {cp if cp is None else f'{cp:.2f}'}   primary_lang: {p['primary_lang']}")
    print(f"  created_at : {p['created_at']}   first commit: {p['first']}")
    print(f"  branches/commits/authors: {p['n_branches']}/{p['n_commits']}/{p['n_authors']}")
    if p["api_error"]:
        print(f"  !! api_error: {p['api_error']}  (contract_pct unavailable)")
    print("\n--- full dict ---")
    pprint.pprint(p, width=100, sort_dicts=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python profile.py <github repo url>")
        raise SystemExit(2)
    _print(profile_repo(sys.argv[1]))
