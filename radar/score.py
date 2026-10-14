"""
Radar v2 scoring core — standalone, testable. v2 (bugs 1 & 2 fixed).
BUYER  = branches UNION commits (some teams name the firm only in commit
         subjects, never in a branch — so commits must count too).
TIMING = branches only + structural -audit match + velocity (crunch) + age.
Bare 'audit' in a branch = wallpaper = ignored.
"""
import re, subprocess, datetime, collections

# v2.1: "-" in a keyword also matches a space, an underscore or nothing.
# "ai-review" stays strict: with spaces it matches AI code-review bots
# ("address ai review comments"), not audits (tested Oct 7, 13 false hits).
STRICT_KW = {"ai-review"}
def kw(k):
    body = re.escape(k) if k in STRICT_KW else re.escape(k).replace(r'\-', r'[\s_-]?')
    return re.compile(r'(?<![a-z0-9])' + body + r'(?![a-z0-9])')

AUDIT_SCOPED = ["pre-audit","preaudit","audit-prep","audit-candidate",
                "security-audit","make-security-audit","audit-scope",
                "code-freeze","codefreeze"]
# structural: a branch whose name ENDS in -audit or contains /audit-scoped forms,
# but NOT a bare 'audit' branch (wallpaper). Catches feat/v0.6.0-audit, refactor/pre-audit.
STRUCTURAL_AUDIT = re.compile(r'(?<![a-z0-9])[a-z0-9._/-]*-audit(?![a-z0-9])|/audit-')
AI_AUDIT     = ["ai-audit","ai-review","agent-audit","autoaudit","llm-audit",
                "self-audit","auditagent"]
AUDIT_FIRMS  = ["bailsec","halborn","certora","openzeppelin","sherlock",
                "code4rena","cantina","spearbit","trail-of-bits","hexens",
                "zellic","quantstamp","consensys","chainsecurity","peckshield",
                "slowmist","immunefi","cyfrin","pashov","ackee","creed",
                "electisec","yaudit","synthra","oz",
                # v2.2 additions (Oct 2026). Removed after testing: "pessimistic"
                # (DeFi jargon), "trust-security" (generic phrase).
                # "runtimeverification" = org name only; the spaced phrase is generic.
                "abdk","veridise","zenith","hacken","zealynx","certik",
                "sigma-prime","runtimeverification","ottersec","kudelski",
                "least-authority","mixbytes","dedaub","guardian-audits",
                "three-sigma","oak-security","sec3",
                "offside-labs","zokyo","omniscia","beosin","blocksec","salus",
                "fuzzland","statemind","chainlight","hashdit","solidproof",
                "cyberscope","decurity","secure3","verichains",
                "0xmacro","nascent","quillaudits","shieldify","codehawks",
                "hats-finance","coinspect","softstack"]
REMEDIATION  = ["audit-fix","audit-fixes","audit-findings","post-audit",
                "remediation","security-review"]

SCOPED=[kw(k) for k in AUDIT_SCOPED]; AIA=[kw(k) for k in AI_AUDIT]
FIRMS=[(k,kw(k)) for k in AUDIT_FIRMS]; REMED=[kw(k) for k in REMEDIATION]
BOTS=("dependabot","github-action","actions-user","noreply@github","[bot]",
      "renovate","snyk","semantic-release","cursoragent","cursor.com",
      "devin","codex@","copilot",
      "claude@anthropic","anthropic.com","noreply@anthropic")

def run(p,c): return subprocess.run(c,shell=True,capture_output=True,text=True,cwd=p).stdout

def load(path):
    raw=run(path,"git log --all --format='%ad|%ae|%s' --date=short")
    commits,authors=[],set()
    for line in raw.strip().split("\n"):
        parts=line.split("|",2)
        if len(parts)<3: continue
        d,ae,s=parts
        try: dt=datetime.date.fromisoformat(d.strip())
        except: continue
        commits.append((dt,s.lower())); ae=ae.strip().lower()
        if ae and not any(b in ae for b in BOTS):
            if "users.noreply.github.com" in ae and "+" in ae: ae=ae.split("+")[1].split("@")[0]
            authors.add(ae)
    branches=[b.strip().lower() for b in
              run(path,"git branch -r --format='%(refname:short)'").strip().split("\n") if b.strip()]
    first=min((d for d,_ in commits),default=None)
    last =max((d for d,_ in commits),default=None)
    return commits,branches,authors,first,last

def any_hit(pats,texts):
    return any(p.search(t) for t in texts for p in pats)
def firm_hits(texts):
    f=set()
    for t in texts:
        for n,p in FIRMS:
            if p.search(t): f.add(n)
    return f

def has_scoped_branch(branches):
    if any_hit(SCOPED,branches): return True
    for b in branches:
        # strip any bare 'audit' branch (wallpaper) — only structural forms count
        if b in ("audit","origin/audit"): continue
        if STRUCTURAL_AUDIT.search(b): return True
    return False

def velocity_spike(commits, today=None):
    """crunch: any of last 12 weeks > 2x median of weeks -26..-13 (anchored on last commit)."""
    if not commits: return False
    anchor=max(d for d,_ in commits)
    wk=collections.Counter()
    for d,_ in commits:
        w=(d-anchor).days//7
        if -26<=w<=0: wk[w]+=1
    base=[wk.get(w,0) for w in range(-26,-13)]
    base_med=sorted(base)[len(base)//2] if base else 0
    recent=[wk.get(w,0) for w in range(-12,1)]
    if base_med==0:
        return max(recent,default=0)>=5   # from-cold burst
    return max(recent,default=0) > 2*base_med

def score_buyer(branches,subs):
    both=branches+subs; pts,hits=0,[]
    f=firm_hits(both)
    if f: pts+=2; hits.append("firms:"+",".join(sorted(f)))
    if any_hit(AIA,both): pts+=2; hits.append("ai-audit")
    if any_hit(REMED,both): pts+=1; hits.append("remediation")
    return min(pts,5),hits

def score_timing(branches,commits,first,today=None):
    # v2.3: default is the real scan date (was frozen at 2026-07-18).
    today=today or datetime.date.today(); pts,hits=0,[]
    if has_scoped_branch(branches): pts+=3; hits.append("audit-scoped-branch")
    if first and (today-first).days<180: pts+=1; hits.append("age<180d")
    if velocity_spike(commits): pts+=1; hits.append("velocity-crunch")
    return min(pts,5),hits

def incumbent_flag(subs,branches):
    c=collections.Counter()
    for t in subs+branches:
        for n,p in FIRMS:
            if p.search(t): c[n]+=1
    inc=[(n,x) for n,x in c.items() if x>=3]
    if inc:
        n,x=max(inc,key=lambda z:z[1]); return f"{n} x{x}"
    return ""

def profile(path,label,today=None):
    commits,branches,authors,first,last=load(path)
    subs=[s for _,s in commits]
    buyer,bh=score_buyer(branches,subs)
    timing,th=score_timing(branches,commits,first,today)
    score=max(1,min(10,buyer+timing))
    return dict(label=label,score=score,buyer=buyer,timing=timing,
                buyer_hits=bh,timing_hits=th,incumbent=incumbent_flag(subs,branches),
                n_branches=len(branches),n_commits=len(commits),
                n_authors=len(authors),first=first,authors=authors)
