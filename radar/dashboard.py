"""
dashboard.py — the BD triage view.

    python dashboard.py [db_path] [out_path]

Reads data/out/radar.db and writes a single self-contained
data/out/dashboard.html:
every byte of CSS and JavaScript is inline, there is no CDN, no font fetch,
no XHR. Double-click the file on a plane and it works.

READ ONLY. This module never writes to the database and never imports
score/profile/store/alert. It opens sqlite in read-only mode where the
platform allows it, so a bug here can never corrupt a scan.

Three sections, in the order a BD morning actually goes:

  1. Movers      — what changed since the previous scan, biggest swing first.
                   Deltas need two scans for a repo; on a virgin DB the
                   section says so instead of rendering an empty table.
  2. Call today  — score >= 7, no incumbent, status 'new'. The short list.
  3. Ranked      — everything, score descending, with live filters.

Contributor emails ride along per repo (collapsed by default) because that is
how contacts get found, and any repo sharing a contributor with a repo you
have marked client/contacted is flagged — warm intro territory.

Stdlib only: sqlite3, json, html, os, datetime. The local imports are paths
(where data lives) and staging, used only for the read-only tag lookup
(staging.latest_tags); both are themselves stdlib-only.
"""
import datetime
import html
import json
import os
import sqlite3
import sys

import paths
import staging as staging_mod

# Same override story as store.py: RADAR_DB moves the database, RADAR_DASHBOARD
# moves the output. Defaults sit side by side in data/out (see paths.py).
DB_PATH = os.environ.get("RADAR_DB") or paths.data_path("out", "radar.db")
OUT_PATH = (os.environ.get("RADAR_DASHBOARD")
            or paths.data_path("out", "dashboard.html"))

# Statuses that mean "already handled" — the hide-checkbox targets these.
SUPPRESSED_STATUSES = ("contacted", "client", "ignore")

# A repo whose contributor also shows up on one of these is a warm intro.
DEAL_STATUSES = ("client", "contacted")

CALL_TODAY_MIN = 7

# Above BIG_TABLE rows the ranked table renders TOP_N first and hides the rest
# behind a toggle, so the page paints instantly on a 400-repo database.
BIG_TABLE = 200
TOP_N = 100


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

def _connect(db_path):
    """Open read-only if the URI form is available, else plain read."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _splithits(s):
    """scans.buyer_hits / timing_hits are semicolon-joined strings."""
    if not s:
        return []
    return [x.strip() for x in s.split(";") if x.strip()]


def collect(conn):
    """Everything the page needs, as plain dicts. No formatting decisions here.

    Per repo we keep the last two scan rows: the latest is the current state,
    the one before it is what 'previous' means for a delta. Comparing a repo's
    own last two rows (rather than two global scan dates) is deliberate — the
    repo list changes between runs, so not every repo is scanned every day, and
    a repo skipped yesterday should still show the move it made the day it was
    last looked at. Its last_scan date is on the row so a stale one is visible.
    """
    repos = {r["repo_id"]: dict(r) for r in conn.execute("SELECT * FROM repos")}

    # Ascending, so the last two appended per repo are prev then latest.
    last_two = {}
    for row in conn.execute(
        "SELECT * FROM scans ORDER BY repo_id, scan_date ASC"
    ):
        last_two.setdefault(row["repo_id"], []).append(dict(row))
        if len(last_two[row["repo_id"]]) > 2:
            last_two[row["repo_id"]].pop(0)

    authors = {}
    for row in conn.execute("SELECT repo_id, email FROM authors ORDER BY email"):
        authors.setdefault(row["repo_id"], []).append(row["email"])

    return repos, last_two, authors


def _shared_deal_contacts(repos, authors):
    """repo_id -> [(deal_repo_name, shared_email), ...].

    Only contributors of repos you have marked client/contacted count. A repo
    is never flagged against itself.
    """
    deal_ids = {
        rid for rid, r in repos.items()
        if (r.get("status") or "") in DEAL_STATUSES
    }
    if not deal_ids:
        return {}

    # email -> deal repo ids that have it
    deal_email = {}
    for rid in deal_ids:
        for email in authors.get(rid, ()):
            deal_email.setdefault(email, set()).add(rid)

    shared = {}
    for rid in repos:
        if rid in deal_ids:
            continue
        hits = {}
        for email in authors.get(rid, ()):
            for deal_id in deal_email.get(email, ()):
                hits.setdefault(deal_id, []).append(email)
        if hits:
            shared[rid] = sorted(
                (repos[d].get("full_name") or str(d), sorted(e))
                for d, e in hits.items()
            )
    return shared


def build_payload(conn, db_path=None):
    """The JSON blob the page renders from."""
    repos, last_two, authors = collect(conn)
    shared = _shared_deal_contacts(repos, authors)
    stag = staging_mod.latest_tags(conn)

    # Unique email table + integer references. 25k author rows collapse to
    # ~13k strings, which is the difference between a snappy file and a fat one.
    email_ix, emails = {}, []

    def ref(email):
        i = email_ix.get(email)
        if i is None:
            i = email_ix[email] = len(emails)
            emails.append(email)
        return i

    out = []
    any_history = False

    for rid, r in repos.items():
        scans = last_two.get(rid) or []
        cur = scans[-1] if scans else {}
        prev = scans[-2] if len(scans) > 1 else None
        if prev is not None:
            any_history = True

        cur_hits = _splithits(cur.get("buyer_hits")) + _splithits(
            cur.get("timing_hits")
        )
        if prev:
            prev_hits = _splithits(prev.get("buyer_hits")) + _splithits(
                prev.get("timing_hits")
            )
            gained = [h for h in cur_hits if h not in prev_hits]
            lost = [h for h in prev_hits if h not in cur_hits]
            changed = [f"+{h}" for h in gained] + [f"-{h}" for h in lost]
        else:
            changed = []

        score = cur.get("score")
        prev_score = prev.get("score") if prev else None
        delta = (
            (score or 0) - (prev_score or 0)
            if score is not None and prev_score is not None
            else None
        )

        out.append({
            "id": rid,
            "name": r.get("full_name") or f"repo {rid}",
            "url": (
                f"https://github.com/{r['full_name']}"
                if r.get("full_name") else f"https://github.com/search?q={rid}"
            ),
            "score": score,
            "buyer": cur.get("buyer"),
            "timing": cur.get("timing"),
            "pct": r.get("contract_pct"),
            "lang": r.get("primary_lang"),
            "inc": (cur.get("incumbent") or "").strip(),
            "status": r.get("status") or "new",
            "why": cur_hits + ([stag[rid]] if rid in stag else []),
            "first_seen": r.get("first_seen") or "",
            "last_scan": cur.get("scan_date") or "",
            "prev": prev_score,
            "prev_date": prev.get("scan_date") if prev else None,
            "delta": delta,
            "changed": changed,
            "authors": [ref(e) for e in authors.get(rid, ())],
            "shared": [
                {"repo": name, "emails": es} for name, es in shared.get(rid, ())
            ],
        })

    out.sort(key=lambda d: (-(d["score"] or 0), d["name"].lower()))

    return {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "db": os.path.abspath(db_path) if db_path else "",
        "emails": emails,
        "repos": out,
        "has_history": any_history,
        "top_n": TOP_N,
        "big_table": BIG_TABLE,
        "call_min": CALL_TODAY_MIN,
        "suppressed": list(SUPPRESSED_STATUSES),
    }


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def _json_for_script(payload):
    """json.dumps, made safe to sit inside a <script> tag."""
    return (
        json.dumps(payload, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


CSS = """
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#16181d; --muted:#6b7280; --line:#e3e6ea;
  --green:#0f8a4a; --green-bg:#e8f7ee; --green-line:#b8e4c9;
  --grey:#8a9099; --accent:#1f5fd0; --chip:#eef1f5;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#14161a; --panel:#1b1e24; --ink:#e7e9ec; --muted:#9aa2ad; --line:#2b2f37;
    --green:#4ad48a; --green-bg:#123024; --green-line:#1f5c3c;
    --grey:#777d87; --accent:#7aa7ff; --chip:#252a32;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1500px;margin:0 auto;padding:22px 20px 80px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:20px;margin:0;letter-spacing:-.2px}
.sub{color:var(--muted);font-size:12px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;margin:26px 0 8px;
  color:var(--muted);display:flex;align-items:center;gap:8px}
h2 .n{background:var(--chip);color:var(--ink);border-radius:20px;padding:1px 8px;
  font-size:11px;letter-spacing:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.note{padding:14px 16px;color:var(--muted);font-style:italic}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:600;padding:9px 10px;border-bottom:1px solid var(--line);
  white-space:nowrap;position:sticky;top:0;background:var(--panel);z-index:2}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tr.hot{background:var(--green-bg)}
tr.hot td:first-child{box-shadow:inset 3px 0 0 var(--green)}
tr.dim{color:var(--grey)}
tr.dim a{color:var(--grey)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.score{font-weight:700;font-variant-numeric:tabular-nums;text-align:right}
.up{color:var(--green);font-weight:700;white-space:nowrap}
.down{color:var(--grey);font-weight:700;white-space:nowrap}
.why{color:var(--muted);font-size:12px;max-width:340px}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 5px;
  margin:1px 3px 1px 0;font-size:11px;white-space:nowrap}
.chip.plus{color:var(--green)}
.chip.minus{color:var(--grey)}
.status{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.warm{display:inline-block;font-size:11px;color:var(--green);border:1px solid var(--green-line);
  border-radius:4px;padding:0 5px;margin-left:6px;white-space:nowrap}
.date{font-size:12px;color:var(--muted);white-space:nowrap}
.repo{font-weight:600}
.contrib-btn{cursor:pointer;background:none;border:1px solid var(--line);border-radius:5px;
  color:var(--muted);font:inherit;font-size:11px;padding:1px 7px;white-space:nowrap}
.contrib-btn:hover{border-color:var(--accent);color:var(--accent)}
tr.detail td{background:var(--bg);font-size:12px;padding:10px 14px 14px}
.emails{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:6px}
.emails code{font-size:11.5px;background:var(--chip);border-radius:4px;padding:1px 5px}
.emails code.bot{opacity:.45}
.detail h4{margin:0 0 2px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.warmbox{margin-bottom:10px;padding:8px 10px;border:1px solid var(--green-line);
  border-radius:6px;background:var(--green-bg)}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0 12px;
  border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.controls label{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);
  white-space:nowrap;cursor:pointer}
.controls input[type=search]{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  color:var(--ink);font:inherit;font-size:13px;padding:5px 9px;min-width:210px}
.controls input[type=range]{width:130px}
.controls .val{font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}
.more{padding:10px 14px;text-align:center}
.more button{cursor:pointer;background:var(--panel);border:1px solid var(--line);border-radius:6px;
  color:var(--accent);font:inherit;font-size:12px;padding:5px 14px}
.legend{font-size:11px;color:var(--muted);margin-top:8px;display:flex;gap:16px;flex-wrap:wrap}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
"""


JS = r"""
var D = window.__RADAR__;
var S = {min:0, hideInc:false, hideDone:false, q:"", showAll:false};

function esc(s){
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}
function pct(v){ return v == null ? "—" : Math.round(v*100) + "%"; }
function isHot(r){ return (r.score||0) >= D.call_min && !r.inc && r.status === "new"; }
function isBot(e){
  return /noreply|users\.noreply\.github\.com|\[bot\]|dependabot|github-actions/i.test(e);
}
function chips(list, cls){
  if(!list || !list.length) return '<span class="chip">—</span>';
  return list.map(function(h){
    var c = cls ? cls : (h[0]==="+" ? "chip plus" : h[0]==="-" ? "chip minus" : "chip");
    return '<span class="'+c+'">'+esc(h)+'</span>';
  }).join("");
}
function deltaCell(d){
  if(d == null) return '<span class="date">—</span>';
  if(d > 0)  return '<span class="up">+'+d+' ▲</span>';
  if(d < 0)  return '<span class="down">'+d+' ▼</span>';
  return '<span class="date">0</span>';
}
function rowClass(r){
  if(r.inc) return "dim";
  if(isHot(r)) return "hot";
  return "";
}
function repoCell(r){
  var h = '<a class="repo" href="'+esc(r.url)+'" target="_blank" rel="noreferrer">'+esc(r.name)+'</a>';
  if(r.shared && r.shared.length) h += '<span class="warm" title="'+esc(r.shared.map(function(s){return s.repo;}).join(", "))+'">shares contributor with known deal</span>';
  return h;
}

function passes(r){
  if((r.score||0) < S.min) return false;
  if(S.hideInc && r.inc) return false;
  if(S.hideDone && D.suppressed.indexOf(r.status) !== -1) return false;
  if(S.q && r.name.toLowerCase().indexOf(S.q) === -1) return false;
  return true;
}

/* ---------- contributor detail ---------- */
function detailHTML(r){
  var h = "";
  if(r.shared && r.shared.length){
    h += '<div class="warmbox"><h4>warm intro</h4>';
    h += r.shared.map(function(s){
      return esc(s.repo)+' &middot; via '+s.emails.map(function(e){return '<code>'+esc(e)+'</code>';}).join(" ");
    }).join("<br>");
    h += '</div>';
  }
  h += '<h4>'+r.authors.length+' contributor'+(r.authors.length===1?"":"s")+'</h4>';
  if(!r.authors.length){
    h += '<div class="date">no author emails captured for this repo</div>';
    return h;
  }
  var list = r.authors.map(function(i){ return D.emails[i]; });
  list.sort(function(a,b){ return (isBot(a)-isBot(b)) || a.localeCompare(b); });
  h += '<div class="emails">'+list.map(function(e){
    return '<code class="'+(isBot(e)?"bot":"")+'">'+esc(e)+'</code>';
  }).join("")+'</div>';
  return h;
}

var byId = {};
D.repos.forEach(function(r){ byId[r.id] = r; });

/* The same repo can appear in Movers, Call today and Ranked at once, so the
   detail row is always attached relative to the button that was clicked —
   never looked up by id, which would open it in the wrong section. */
function toggleDetail(btn, cols){
  var tr = btn.parentNode;
  while(tr && tr.tagName !== "TR"){ tr = tr.parentNode; }
  if(!tr) return;
  var nxt = tr.nextElementSibling;
  if(nxt && nxt.className.indexOf("detail") !== -1){
    nxt.parentNode.removeChild(nxt);
    return;
  }
  var r = byId[btn.getAttribute("data-id")];
  if(!r) return;
  var d = document.createElement("tr");
  d.className = "detail";
  d.innerHTML = '<td colspan="'+cols+'">'+detailHTML(r)+'</td>';
  tr.parentNode.insertBefore(d, tr.nextSibling);
}

function wireDetails(scope, cols){
  var btns = scope.getElementsByClassName("contrib-btn");
  for(var i = 0; i < btns.length; i++){
    (function(b){
      b.onclick = function(){ toggleDetail(b, cols); };
    })(btns[i]);
  }
}

/* ---------- sections ---------- */
function renderMovers(){
  var el = document.getElementById("movers");
  if(!D.has_history){
    el.innerHTML = '<div class="note">baseline scan — deltas appear next run</div>';
    document.getElementById("movers-n").textContent = "0";
    return;
  }
  var rows = D.repos.filter(function(r){ return r.delta != null && r.delta !== 0 && passes(r); });
  rows.sort(function(a,b){
    return Math.abs(b.delta) - Math.abs(a.delta) || (b.score||0)-(a.score||0);
  });
  document.getElementById("movers-n").textContent = rows.length;
  if(!rows.length){
    el.innerHTML = '<div class="note">no score changes since the previous scan'
      + (S.min||S.q||S.hideInc||S.hideDone ? ' (with current filters)' : '') + '</div>';
    return;
  }
  var h = '<table><thead><tr><th>repo</th><th class="num">now</th><th class="num">was</th>'
        + '<th class="num">delta</th><th>signals changed</th><th>last scan</th><th></th></tr></thead><tbody>';
  rows.forEach(function(r){
    h += '<tr data-id="'+r.id+'" class="'+rowClass(r)+'">'
       + '<td>'+repoCell(r)+'</td>'
       + '<td class="score">'+(r.score==null?"—":r.score)+'</td>'
       + '<td class="num date">'+(r.prev==null?"—":r.prev)+'</td>'
       + '<td class="num">'+deltaCell(r.delta)+'</td>'
       + '<td class="why">'+chips(r.changed)+'</td>'
       + '<td class="date">'+esc(r.last_scan)+'</td>'
       + '<td><button class="contrib-btn" data-id="'+r.id+'">'+r.authors.length+' contributors</button></td>'
       + '</tr>';
  });
  el.innerHTML = h + '</tbody></table>';
  wireDetails(el, 7);
}

function renderCallToday(){
  var el = document.getElementById("calltoday");
  var rows = D.repos.filter(function(r){ return isHot(r) && passes(r); });
  document.getElementById("call-n").textContent = rows.length;
  if(!rows.length){
    el.innerHTML = '<div class="note">nothing at score '+D.call_min+'+ that is clean and untouched right now</div>';
    return;
  }
  var h = '<table><thead><tr><th class="num">score</th><th>repo</th><th>why</th>'
        + '<th class="num">contract%</th><th>first seen</th><th>delta</th><th></th></tr></thead><tbody>';
  rows.forEach(function(r){
    h += '<tr data-id="'+r.id+'" class="hot">'
       + '<td class="score">'+r.score+'</td>'
       + '<td>'+repoCell(r)+'</td>'
       + '<td class="why">'+chips(r.why,"chip")+'</td>'
       + '<td class="num">'+pct(r.pct)+'</td>'
       + '<td class="date">'+esc(r.first_seen)+'</td>'
       + '<td>'+deltaCell(r.delta)+'</td>'
       + '<td><button class="contrib-btn" data-id="'+r.id+'">'+r.authors.length+' contributors</button></td>'
       + '</tr>';
  });
  el.innerHTML = h + '</tbody></table>';
  wireDetails(el, 7);
}

function renderRanked(){
  var el = document.getElementById("ranked");
  var rows = D.repos.filter(passes);
  document.getElementById("ranked-n").textContent = rows.length;
  var capped = D.repos.length > D.big_table && !S.showAll && rows.length > D.top_n;
  var shown = capped ? rows.slice(0, D.top_n) : rows;
  if(!rows.length){
    el.innerHTML = '<div class="note">no repos match these filters</div>';
    return;
  }
  var h = '<table><thead><tr>'
        + '<th class="num">score</th><th class="num">buy</th><th class="num">tim</th>'
        + '<th class="num">contract%</th><th>incumbent</th><th>status</th><th>repo</th>'
        + '<th>why</th><th>first seen</th><th>last scan</th><th>contributors</th>'
        + '</tr></thead><tbody>';
  shown.forEach(function(r){
    h += '<tr data-id="'+r.id+'" class="'+rowClass(r)+'">'
       + '<td class="score">'+(r.score==null?"—":r.score)+'</td>'
       + '<td class="num">'+(r.buyer==null?"—":r.buyer)+'</td>'
       + '<td class="num">'+(r.timing==null?"—":r.timing)+'</td>'
       + '<td class="num">'+pct(r.pct)+'</td>'
       + '<td class="date">'+(r.inc ? esc(r.inc) : "—")+'</td>'
       + '<td class="status">'+esc(r.status)+'</td>'
       + '<td>'+repoCell(r)+'</td>'
       + '<td class="why">'+chips(r.why,"chip")+'</td>'
       + '<td class="date">'+esc(r.first_seen)+'</td>'
       + '<td class="date">'+esc(r.last_scan)+'</td>'
       + '<td><button class="contrib-btn" data-id="'+r.id+'">'+r.authors.length+'</button></td>'
       + '</tr>';
  });
  h += '</tbody></table>';
  if(capped){
    h += '<div class="more"><button id="showall">show all '+rows.length+' repos</button></div>';
  } else if(S.showAll && D.repos.length > D.big_table){
    h += '<div class="more"><button id="showtop">show top '+D.top_n+' only</button></div>';
  }
  el.innerHTML = h;
  wireDetails(el, 11);
  var sa = document.getElementById("showall");
  if(sa) sa.onclick = function(){ S.showAll = true; renderRanked(); };
  var st = document.getElementById("showtop");
  if(st) st.onclick = function(){ S.showAll = false; renderRanked(); };
}

function renderAll(){ renderMovers(); renderCallToday(); renderRanked(); }

/* ---------- controls ---------- */
document.getElementById("min").oninput = function(e){
  S.min = +e.target.value;
  document.getElementById("minval").textContent = S.min;
  renderAll();
};
document.getElementById("hideinc").onchange = function(e){
  S.hideInc = e.target.checked; renderAll();
};
document.getElementById("hidedone").onchange = function(e){
  S.hideDone = e.target.checked; renderAll();
};
document.getElementById("q").oninput = function(e){
  S.q = e.target.value.trim().toLowerCase(); renderAll();
};

renderAll();
"""


def render(payload):
    n = len(payload["repos"])
    hot = sum(
        1 for r in payload["repos"]
        if (r["score"] or 0) >= payload["call_min"] and not r["inc"]
        and r["status"] == "new"
    )
    latest = max((r["last_scan"] for r in payload["repos"] if r["last_scan"]),
                 default="—")
    max_score = max((r["score"] or 0) for r in payload["repos"]) if n else 10

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar — BD triage</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">
<header>
  <h1>Radar</h1>
  <span class="sub">{n} repos &middot; latest scan {html.escape(latest)}
    &middot; {hot} on the call list &middot; built {html.escape(payload['generated'])}</span>
</header>

<div class="controls">
  <label>min score <input type="range" id="min" min="0" max="{max(max_score, 1)}" value="0" step="1">
    <span class="val" id="minval">0</span></label>
  <label><input type="checkbox" id="hideinc"> hide incumbent</label>
  <label><input type="checkbox" id="hidedone"> hide contacted / client / ignore</label>
  <label><input type="search" id="q" placeholder="search repo name…" spellcheck="false"></label>
</div>

<h2>Movers <span class="n" id="movers-n">0</span></h2>
<div class="panel" id="movers"></div>

<h2>Call today <span class="n" id="call-n">0</span></h2>
<div class="panel" id="calltoday"></div>

<h2>Ranked <span class="n" id="ranked-n">0</span></h2>
<div class="panel" id="ranked"></div>

<div class="legend">
  <span><span class="sw" style="background:var(--green)"></span>score &ge; {payload['call_min']},
    no incumbent, status new — call these</span>
  <span><span class="sw" style="background:var(--grey)"></span>incumbent flagged — dimmed</span>
  <span>click a contributor count to see the emails</span>
</div>
</div>
<script>window.__RADAR__ = {_json_for_script(payload)};</script>
<script>{JS}</script>
</body></html>
"""


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build(db_path=None, out_path=None):
    """Read the DB, write the HTML, return a small summary dict.

    Never raises for a missing database — a fresh checkout with no scan yet
    should not take the whole run down with it.
    """
    db_path = db_path or DB_PATH
    out_path = out_path or OUT_PATH

    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"no database at {db_path}")

    conn = _connect(db_path)
    try:
        payload = build_payload(conn, db_path)
    finally:
        conn.close()

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render(payload))

    return {
        "path": os.path.abspath(out_path),
        "repos": len(payload["repos"]),
        "emails": len(payload["emails"]),
        "movers": sum(
            1 for r in payload["repos"] if r["delta"] not in (None, 0)
        ),
        "call_today": sum(
            1 for r in payload["repos"]
            if (r["score"] or 0) >= payload["call_min"] and not r["inc"]
            and r["status"] == "new"
        ),
        "has_history": payload["has_history"],
        "bytes": os.path.getsize(out_path),
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    db = argv[0] if len(argv) > 0 else None
    out = argv[1] if len(argv) > 1 else None
    info = build(db, out)
    print(
        f"dashboard: {info['path']}  ({info['repos']} repos, "
        f"{info['movers']} movers, {info['call_today']} to call, "
        f"{info['bytes']/1024:.0f} KB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
