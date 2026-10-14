"""
langgate.py — the cheap language gate.

Takes a file of GitHub repo URLs (one per line) and decides which ones look
like smart-contract repos WITHOUT cloning anything. The only network call per
repo is GET /repos/{owner}/{name}/languages, which returns the byte count of
each detected language.

    contract_pct = bytes in CONTRACT_LANGS / total bytes

This is the same definition profile.py uses (and the same CONTRACT_LANGS set),
but reached without a clone, so it can be run over a few thousand discovered
URLs in one pass to decide which are worth the expensive path.

Nothing here is enforced — the threshold below is only used for the summary
line. The CSV carries the raw number so a threshold can be picked afterwards.

Stdlib only: urllib for the API, csv for the output.

usage:
    python langgate.py repos_discovered.txt [-o langgate_results.csv]
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Languages that indicate on-chain code. Same set as profile.py.
CONTRACT_LANGS = {"Solidity", "Vyper", "Cairo", "Move", "Noir", "FunC", "Michelson"}

GITHUB_API = "https://api.github.com"
UA = "github-radar-v2-langgate"

# Reported in the summary only. Not applied to the CSV.
PASS_THRESHOLD = 0.45

# When the remaining quota drops to this, sleep until the window resets rather
# than spending the last few calls and getting 403'd mid-run.
RATE_FLOOR = 5
# Longest we will ever sleep waiting for a reset, as a guard against a bogus
# or far-future X-RateLimit-Reset header.
MAX_SLEEP = 3900


# --------------------------------------------------------------------------
# env / token  (same behaviour as profile.py: shell env wins, .env is fallback)
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
# url parsing  (same regex as profile.py)
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


def read_urls(path):
    """One URL per line. Blank lines, '#' comments and repeats are dropped."""
    urls, seen = [], set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line in seen:
                continue
            seen.add(line)
            urls.append(line)
    return urls


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

class RateLimiter:
    """Tracks the X-RateLimit-* headers and parks the run before the quota
    actually hits zero.

    GitHub sends the remaining quota and the reset epoch on every response,
    including error responses, so we learn the state from whatever came back
    last and never have to spend a call asking.
    """

    def __init__(self, floor=RATE_FLOOR, verbose=True):
        self.floor = floor
        self.verbose = verbose
        self.remaining = None
        self.reset_at = None
        self.sleeps = 0

    def observe(self, headers):
        try:
            v = headers.get("X-RateLimit-Remaining")
            if v is not None:
                self.remaining = int(v)
        except (TypeError, ValueError):
            pass
        try:
            v = headers.get("X-RateLimit-Reset")
            if v is not None:
                self.reset_at = int(v)
        except (TypeError, ValueError):
            pass

    def _sleep_for(self):
        """Seconds to wait for the window to roll over, clamped."""
        if not self.reset_at:
            return 60
        return max(0, min(int(self.reset_at - time.time()) + 3, MAX_SLEEP))

    def wait_if_low(self):
        """Called before each request. Parks if the quota is nearly spent."""
        if self.remaining is None or self.remaining > self.floor:
            return
        naptime = self._sleep_for()
        if naptime <= 0:
            self.remaining = None  # window has rolled; let the next call re-learn
            return
        self.sleeps += 1
        if self.verbose:
            when = time.strftime("%H:%M:%S", time.localtime(self.reset_at or 0))
            print(
                f"  [rate limit] {self.remaining} calls left — sleeping "
                f"{naptime}s until {when}",
                flush=True,
            )
        time.sleep(naptime)
        self.remaining = None

    def wait_for_reset(self):
        """Called after a 403/429 that looks like exhaustion."""
        naptime = self._sleep_for() or 60
        self.sleeps += 1
        if self.verbose:
            print(f"  [rate limit] blocked — sleeping {naptime}s", flush=True)
        time.sleep(naptime)
        self.remaining = None


# --------------------------------------------------------------------------
# the one API call
# --------------------------------------------------------------------------

def _request(path, token, timeout=30):
    req = urllib.request.Request(GITHUB_API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", UA)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), r.headers


def fetch_languages(owner, repo, token, limiter, timeout=30):
    """-> (langs_dict, status).

    status is 'ok' on success, otherwise a short reason string. Every failure
    mode returns rather than raises: one dead repo must never kill the run.
    """
    path = f"/repos/{owner}/{repo}/languages"
    retried_rate = False
    for attempt in range(3):
        limiter.wait_if_low()
        try:
            langs, headers = _request(path, token, timeout=timeout)
            limiter.observe(headers)
            if not isinstance(langs, dict):
                return {}, "error:bad-payload"
            return langs, "ok"

        except urllib.error.HTTPError as e:
            limiter.observe(e.headers or {})
            if e.code == 404:
                return {}, "404"                     # deleted, renamed, or private
            if e.code in (403, 429):
                hdrs = e.headers or {}
                retry_after = hdrs.get("Retry-After")
                has_quota_hdrs = hdrs.get("X-RateLimit-Remaining") is not None
                exhausted = has_quota_hdrs and limiter.remaining is not None \
                    and limiter.remaining <= 0

                # Not every 403 is a rate limit. A blocked/suspended account, a
                # token without the right scope, or an egress proxy that gates
                # which repos are reachable all answer 403 with NO X-RateLimit-*
                # and no Retry-After. Sleeping on those burns the whole run
                # waiting for a window that was never closed, so treat a 403
                # with no rate-limit evidence as a plain refusal and move on.
                if not (exhausted or retry_after or (has_quota_hdrs and e.code == 429)):
                    return {}, "403:forbidden"

                if not retried_rate:
                    retried_rate = True
                    if retry_after and not exhausted:
                        # Secondary/abuse limiter: it tells us how long to wait.
                        try:
                            nap = max(1, min(int(retry_after), 300))
                        except ValueError:
                            nap = 60
                        print(f"  [rate limit] secondary limit — sleeping {nap}s",
                              flush=True)
                        time.sleep(nap)
                    else:
                        limiter.wait_for_reset()
                    continue
                return {}, "403:rate-limited"
            if e.code in (451, 409):
                # 451 = DMCA takedown, 409 = empty repository (no commits yet)
                return {}, ("empty" if e.code == 409 else "451:unavailable")
            if 500 <= e.code < 600 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return {}, f"error:HTTP {e.code}"

        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return {}, f"error:{type(e).__name__}"
        except Exception as e:                       # malformed JSON, socket, proxy
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return {}, f"error:{type(e).__name__}"

    return {}, "error:retries-exhausted"


def breakdown(langs):
    """-> (contract_pct, primary_lang, total_bytes). Same math as profile.py."""
    total = sum(langs.values())
    if not total:
        return 0.0, None, 0
    contract = sum(b for lang, b in langs.items() if lang in CONTRACT_LANGS)
    primary = max(langs.items(), key=lambda kv: kv[1])[0]
    return contract / total, primary, total


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def check_repo(url, token, limiter):
    """One row of the CSV. Never raises."""
    row = {
        "repo_url": url,
        "contract_pct": None,
        "primary_lang": "",
        "total_bytes": 0,
        "status": "",
    }
    try:
        owner, repo = parse_owner_repo(url)
    except ValueError:
        row["status"] = "error:unparseable-url"
        return row

    langs, status = fetch_languages(owner, repo, token, limiter)
    if status != "ok":
        row["status"] = status
        if status == "empty":
            # Repo resolved fine, it just has no code (HTTP 409, no commits).
            # That is a real 0%, not a lookup failure — record it as such so it
            # doesn't sort in with the 404s.
            row["contract_pct"] = 0.0
        return row

    pct, primary, total = breakdown(langs)
    if total == 0:
        row["status"] = "empty"                      # repo exists, no detected code
        row["contract_pct"] = 0.0
        return row

    row["contract_pct"] = round(pct, 4)
    row["primary_lang"] = primary or ""
    row["total_bytes"] = total
    row["status"] = "ok"
    return row


def run(input_path, out_path, threshold=PASS_THRESHOLD, limit=None, quiet=False):
    urls = read_urls(input_path)
    if limit:
        urls = urls[:limit]
    if not urls:
        print(f"no URLs found in {input_path}")
        return 1

    token = _token()
    if not token:
        print("!! no GITHUB_TOKEN found (env or .env) — unauthenticated requests "
              "are capped at 60/hour, this will crawl", file=sys.stderr)

    limiter = RateLimiter(verbose=True)
    rows = []
    n = len(urls)
    print(f"checking {n} repos via /languages (no clones)\n")

    for i, url in enumerate(urls, 1):
        row = check_repo(url, token, limiter)
        rows.append(row)

        if row["status"] == "ok":
            if not quiet:
                print(f"[{i}/{n}] {row['contract_pct']:.2f}  "
                      f"{row['primary_lang'] or '-':<14} {url}", flush=True)
        else:
            # Warnings always print, even in --quiet: these are the rows that
            # need a human to look at them.
            print(f"[{i}/{n}] !! {row['status']:<18} {url}", flush=True)

    # Failed rows have no percentage; sort them to the bottom rather than
    # letting a None masquerade as 0.0 at the top of the file.
    rows.sort(
        key=lambda r: (-1.0 if r["contract_pct"] is None else r["contract_pct"],
                       r["total_bytes"]),
        reverse=True,
    )

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["repo_url", "contract_pct", "primary_lang", "total_bytes", "status"])
        for r in rows:
            w.writerow([
                r["repo_url"],
                "" if r["contract_pct"] is None else f"{r['contract_pct']:.4f}",
                r["primary_lang"],
                r["total_bytes"],
                r["status"],
            ])

    _summary(rows, out_path, threshold, limiter)
    return 0


def _summary(rows, out_path, threshold, limiter):
    ok = [r for r in rows if r["status"] in ("ok", "empty")]
    failed = [r for r in rows if r["status"] not in ("ok", "empty")]
    passed = [r for r in ok if (r["contract_pct"] or 0.0) >= threshold]
    zero = [r for r in ok if (r["contract_pct"] or 0.0) == 0.0]
    partial = [r for r in ok if 0.0 < (r["contract_pct"] or 0.0) < threshold]
    not_found = [r for r in failed if r["status"] == "404"]
    limited = [r for r in failed if r["status"] == "403:rate-limited"]
    forbidden = [r for r in failed if r["status"] == "403:forbidden"]
    other = [r for r in failed
             if r["status"] not in ("404", "403:rate-limited", "403:forbidden")]

    print("\n" + "=" * 62)
    print(f"  checked          {len(rows)}")
    print(f"  passed >= {threshold:.2f}   {len(passed)}"
          f"   ({_pct(len(passed), len(rows))})")
    print(f"  partial 0<x<{threshold:.2f} {len(partial)}"
          f"   ({_pct(len(partial), len(rows))})   below the gate, worth an eyeball")
    print(f"  zero contract    {len(zero)}   ({_pct(len(zero), len(rows))})"
          f"   not contract repos at all")
    print(f"  failed           {len(failed)}   ({_pct(len(failed), len(rows))})"
          f"   404:{len(not_found)}  rate-limited:{len(limited)}"
          f"  forbidden:{len(forbidden)}  other:{len(other)}")
    if limiter.sleeps:
        print(f"  rate-limit naps  {limiter.sleeps}")
    print("=" * 62)
    print(f"wrote {out_path}")


def _pct(part, whole):
    return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("input", help="file of GitHub repo URLs, one per line")
    ap.add_argument("-o", "--out", default="langgate_results.csv",
                    help="output CSV (default: langgate_results.csv)")
    ap.add_argument("-t", "--threshold", type=float, default=PASS_THRESHOLD,
                    help=f"pass threshold for the summary (default {PASS_THRESHOLD})")
    ap.add_argument("-n", "--limit", type=int, default=None,
                    help="only check the first N URLs (smoke test)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print warnings, not every repo")
    a = ap.parse_args(argv)
    return run(a.input, a.out, threshold=a.threshold, limit=a.limit, quiet=a.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
