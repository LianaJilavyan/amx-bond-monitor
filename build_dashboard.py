#!/usr/bin/env python3
"""
Builds docs/index.html from data/history.csv.

Self-contained: all data is embedded, no network calls at view time, so it works
on GitHub Pages, from a shared drive, or as an email attachment.

Field names in the AMX feed are not documented. config.json maps logical names
onto whatever the API actually returns; run `fetch.py discover` to see the real
names. Anything unmapped is reported as a gap on the page rather than guessed.
"""

import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import analytics as an
import amx_schema as ax

# Windows consoles often default to cp1252, which cannot print Armenian issuer
# names. Without this, a long backfill can die partway through on a name rather
# than on anything that matters.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"



def read_instruments():
    """Static terms, one row per ISIN, joined onto history rows as i_ fields."""
    p = DATA / "instruments.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig", newline="") as f:
        return {r["isin"]: {f"i_{k}": v for k, v in r.items() if v not in (None, "")}
                for r in csv.DictReader(f) if r.get("isin")}


def read_history():
    p = DATA / "history.csv"
    if not p.exists():
        return []
    inst = read_instruments()
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        terms = inst.get(r.get("isin"))
        if terms:
            for k, v in terms.items():
                r.setdefault(k, v)
            # the board's maturity_date is authoritative; fall back to the terms
            if not r.get("maturity_date"):
                r["maturity_date"] = terms.get("i_maturity_date")
    return rows


def build():
    rows = read_history()
    DOCS.mkdir(parents=True, exist_ok=True)

    if not rows:
        DOCS.joinpath("index.html").write_text(
            render([], [], {"latest": None, "n_days": 0, "generated": "",
                            "gaps": ["No data yet — run fetch.py"]}), encoding="utf-8")
        print("no history yet; wrote placeholder page")
        return

    dates = sorted({r.get("snapshot_date") for r in rows if r.get("snapshot_date")})
    latest = dates[-1]
    today = an.parse_date(latest) or date.today()

    series = {}
    for r in rows:
        isin, d = r.get("isin"), r.get("snapshot_date")
        if isin and d:
            series.setdefault(isin, {})[d] = r

    gaps, bonds = set(), []
    for r in [x for x in rows if x.get("snapshot_date") == latest]:
        if not r.get("isin"):
            continue
        b = ax.normalise(r, today)
        if b["price"] is None:
            gaps.add("price (no quote, no trade)")
        if b["coupon"] is None:
            gaps.add("coupon rate")
        if b["maturity"] is None:
            gaps.add("maturity date")

        hist = series.get(b["isin"], {})
        hdates = sorted(hist)
        cache = {}

        def at(d):
            if d not in cache:
                cache[d] = ax.normalise(hist[d], an.parse_date(d) or today)
            return cache[d]

        earlier = [d for d in hdates if d < latest]
        prev = at(earlier[-1]) if earlier else None
        b["ytm_chg_bp"] = (round((b["ytm"] - prev["ytm"]) * 100, 1)
                           if b["ytm"] is not None and prev and prev["ytm"] is not None else None)

        stale, seen = None, None
        for d in reversed(hdates):
            pv = at(d)["price"]
            if pv is None:
                continue
            if seen is None:
                seen = pv
            elif pv != seen:
                ld = an.parse_date(d)
                if ld:
                    stale = (today - ld).days
                break
        if b.get("days_since_trade") is not None:
            stale = b["days_since_trade"] if stale is None else min(stale, b["days_since_trade"])
        b["stale"] = stale

        b["ts"] = [{"d": d, "p": at(d)["price"], "y": at(d)["ytm"]}
                   for d in hdates if at(d)["price"] is not None]
        bonds.append(b)

    meta = {"latest": latest, "n_days": len(dates), "gaps": sorted(gaps),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    DOCS.joinpath("index.html").write_text(render(bonds, dates, meta), encoding="utf-8")

    ccy, cls = {}, {}
    for b in bonds:
        ccy[b["ccy"]] = ccy.get(b["ccy"], 0) + 1
        cls[b["issuer_class"]] = cls.get(b["issuer_class"], 0) + 1
    print(f"dashboard: {len(bonds)} bonds, {len(dates)} days -> docs/index.html")
    print("  currencies:  " + ", ".join(f"{k} {v}" for k, v in sorted(ccy.items())))
    print("  issuer type: " + ", ".join(f"{k} {v}" for k, v in sorted(cls.items())))
    if gaps:
        print("  gaps: " + "; ".join(sorted(gaps)))


def render(bonds, dates, meta):
    payload = json.dumps({"bonds": bonds, "dates": dates, "meta": meta},
                         ensure_ascii=False, separators=(",", ":"))
    return HTML.replace("/*__DATA__*/", payload)


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMX Corporate Bonds</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f7f4ef; --surface:#ffffff; --raised:#f2ede4; --line:#e2d9cc; --line-soft:#efe8dd;
  --ink:#2b241c; --muted:#7b6b57; --faint:#a6957f;
  --apricot:#a85d13; --apricot-soft:#e9a64a; --apricot-wash:#fbf1e2;
  --tighten:#1f7a54; --widen:#b03a2e; --stale:#bdb0a0;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1400px;margin:0 auto;padding:32px 24px 80px}
h1{font-family:Archivo,sans-serif;font-weight:800;font-size:clamp(28px,4vw,44px);
  letter-spacing:-.02em;margin:0;text-transform:uppercase}
.sub{color:var(--muted);font-family:"IBM Plex Mono",monospace;font-size:12px;
  letter-spacing:.08em;text-transform:uppercase;margin-top:6px}
header{border-bottom:2px solid var(--ink);padding-bottom:24px;margin-bottom:28px}
.lede{margin-top:18px;font-size:17px;max-width:62ch;color:var(--ink)}
.lede b{color:var(--apricot);font-weight:500}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);margin-bottom:28px}
.card{background:var(--surface);padding:16px 18px}
.card .k{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--muted)}
.card .v{font-family:Archivo,sans-serif;font-weight:700;font-size:26px;margin-top:6px;
  letter-spacing:-.01em}
.card .n{font-size:11px;color:var(--faint);margin-top:2px}

.panel{background:var(--surface);border:1px solid var(--line);margin-bottom:24px}
.panel h2{font-family:Archivo,sans-serif;font-size:13px;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;margin:0;padding:14px 18px;border-bottom:1px solid var(--line);
  color:var(--muted)}
.panel .body{padding:18px}

.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:14px 18px;
  border-bottom:1px solid var(--line);background:var(--raised)}
.disclosure{border-top:1px solid var(--line);background:var(--raised);
  padding:12px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
button.disclose{display:inline-flex;align-items:center;gap:9px;cursor:pointer;
  background:var(--surface);border:1px solid var(--line);color:var(--ink);
  font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.08em;
  text-transform:uppercase;padding:9px 14px;border-radius:3px;transition:border-color .12s}
button.disclose:hover{border-color:var(--apricot);color:var(--apricot)}
button.disclose .chev{display:inline-block;transition:transform .16s;font-size:10px}
button.disclose[aria-expanded="true"] .chev{transform:rotate(180deg)}
button.disclose[aria-expanded="true"]{border-color:var(--apricot);color:var(--apricot)}
#tableWrap[hidden]{display:none}
select,input[type=search]{background:var(--surface);color:var(--ink);border:1px solid var(--line);
  padding:7px 10px;font-family:"IBM Plex Mono",monospace;font-size:12px;border-radius:2px}
select:hover,input:hover{border-color:var(--apricot)}
select:focus,input:focus,button:focus-visible,tr:focus-visible{outline:2px solid var(--apricot);outline-offset:1px}
.toggle{display:flex;align-items:center;gap:7px;font-family:"IBM Plex Mono",monospace;
  font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer}
.toggle input{accent-color:var(--apricot)}

svg{display:block;width:100%;height:auto;overflow:visible}
.grid line{stroke:var(--line-soft)}
.ax{fill:var(--faint);font-family:"IBM Plex Mono",monospace;font-size:10px}
.axlab{fill:var(--muted);font-family:"IBM Plex Mono",monospace;font-size:10px;
  letter-spacing:.1em;text-transform:uppercase}
.dot{cursor:pointer;transition:r .12s}
.dot:hover{r:7}

table{width:100%;border-collapse:collapse;font-family:"IBM Plex Mono",monospace;font-size:12px}
th{text-align:right;padding:9px 10px;border-bottom:2px solid var(--line);color:var(--muted);
  font-weight:500;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  cursor:pointer;user-select:none;white-space:nowrap;position:sticky;top:0;background:var(--surface)}
th:first-child,td:first-child,th.l,td.l{text-align:left}
th:hover{color:var(--apricot)}
td{padding:8px 10px;border-bottom:1px solid var(--line-soft);text-align:right;white-space:nowrap}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--apricot-wash)}
tr.is-stale td{color:var(--stale)}
tr.is-stale td.l{color:var(--stale)}
.up{color:var(--widen)} .dn{color:var(--tighten)}
.tag{font-size:9px;letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;
  border:1px solid var(--line);border-radius:2px;color:var(--muted);background:var(--raised)}
.scroll{max-height:620px;overflow:auto}

.freshbar{display:inline-block;width:34px;height:4px;background:var(--line);
  vertical-align:middle;position:relative;border-radius:1px}
.freshbar i{position:absolute;inset:0 auto 0 0;background:var(--apricot);border-radius:1px}

#tip{position:fixed;pointer-events:none;background:var(--ink);color:#fff;border:none;box-shadow:0 4px 14px rgba(43,36,28,.18);
  padding:10px 12px;font-family:"IBM Plex Mono",monospace;font-size:11px;line-height:1.6;
  opacity:0;transition:opacity .1s;z-index:10;max-width:280px;border-radius:2px}
#tip b{color:var(--apricot-soft);font-weight:600}

dialog{background:var(--surface);color:var(--ink);border:1px solid var(--line);box-shadow:0 18px 50px rgba(43,36,28,.22);
  max-width:760px;width:92vw;padding:0;border-radius:2px}
dialog::backdrop{background:rgba(43,36,28,.35)}
dialog header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
  padding:18px;border-bottom:1px solid var(--line);margin:0}
dialog h3{font-family:Archivo,sans-serif;font-size:18px;margin:0;letter-spacing:-.01em}
dialog .body{padding:18px}
button.x{background:var(--surface);border:1px solid var(--line);color:var(--muted);cursor:pointer;
  font-family:"IBM Plex Mono",monospace;font-size:11px;padding:5px 10px;border-radius:2px}
button.x:hover{border-color:var(--apricot);color:var(--apricot)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:14px;
  font-family:"IBM Plex Mono",monospace;font-size:12px;margin-bottom:18px}
.kv div span{display:block;font-size:9px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:3px}
.warn{border-left:3px solid var(--apricot);padding:10px 14px;background:var(--apricot-wash);
  font-size:12px;color:var(--muted);margin-bottom:22px}
.freshness{display:inline-flex;align-items:center;gap:8px;margin-top:10px;
  font-family:"IBM Plex Mono",monospace;font-size:12px;padding:6px 12px;border-radius:20px;
  border:1px solid var(--line);background:var(--surface)}
.freshness .pip{width:8px;height:8px;border-radius:50%;background:var(--tighten);flex:none}
.freshness.warn-age{border-color:var(--apricot);background:var(--apricot-wash);color:var(--apricot)}
.freshness.warn-age .pip{background:var(--apricot)}
.freshness.bad-age{border-color:var(--widen);background:#fdf0ee;color:var(--widen)}
.freshness.bad-age .pip{background:var(--widen)}
.freshness b{font-weight:600}
footer{color:var(--faint);font-size:11px;font-family:"IBM Plex Mono",monospace;
  border-top:1px solid var(--line);padding-top:18px;margin-top:36px;line-height:1.7}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-family:"IBM Plex Mono",monospace;
  font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;
  padding:0 18px 14px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;
  vertical-align:-1px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media(max-width:640px){.wrap{padding:20px 14px 60px}.scroll{max-height:460px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>AMX Corporate Bonds</h1>
  <div class="sub" id="stamp"></div>
  <div class="freshness" id="fresh"><span class="pip"></span><span id="freshText"></span></div>
  <p class="lede" id="lede"></p>
</header>

<div id="gapwarn"></div>
<div class="cards" id="cards"></div>

<div class="panel">
  <h2>Yield against maturity</h2>
  <div class="controls">
    <input type="search" id="q" placeholder="Search issuer or ISIN" style="min-width:180px">
    <select id="fBasis" title="Which side of the quote to price from">
      <option value="bid" selected>Bid yield</option>
      <option value="ask">Ask yield — what a buyer earns</option>
      <option value="mid">Mid yield</option>
      <option value="last">Last traded yield</option>
    </select>
    <select id="fCcy" title="Currency"></select>
    <select id="fClass" title="Issuer type"><option value="">All issuer types</option></select>
    <select id="fType"><option value="">All bond types</option></select>
    <select id="fMat">
      <option value="">All maturities</option>
      <option value="0-1">Under 1 year</option><option value="1-3">1 to 3 years</option>
      <option value="3-5">3 to 5 years</option><option value="5-99">Over 5 years</option>
    </select>
    <label class="toggle"><input type="checkbox" id="hideStale">Hide stale</label>
  </div>
  <div class="legend">
    <span><i style="background:var(--apricot-soft)"></i>Priced this week</span>
    <span><i style="background:none;border:1.5px solid var(--stale)"></i>Stale — mark unchanged 20 days or more</span>
    <span>Dot size = amount outstanding</span>
  </div>
  <div class="body"><svg id="scatter" viewBox="0 0 900 420" role="img"
    aria-label="Scatter plot of yield to maturity against years to maturity"></svg></div>
  <div class="disclosure">
    <button class="disclose" id="toggleTable" aria-expanded="false" aria-controls="tableWrap">
      <span class="chev" aria-hidden="true">&#9662;</span><span class="lbl">Show instruments</span>
    </button>
    <span class="ccy-note" id="count"></span>
  </div>
</div>

<div class="panel" id="tableWrap" hidden>
  <h2>All instruments</h2>
  <div class="scroll"><table id="tbl">
    <thead><tr>
      <th class="l" data-k="isin">ISIN</th><th class="l" data-k="issuer">Issuer</th>
      <th class="l" data-k="issuer_class">Issuer type</th>
      <th class="l" data-k="type">Bond type</th><th data-k="ccy">Ccy</th>
      <th data-k="coupon">Coupon</th><th data-k="maturity">Maturity</th>
      <th data-k="yrs">Years</th><th data-k="price">Mark</th>
      <th data-k="spread_bp">Bid-ask</th>
      <th data-k="ytm" id="thYtm">Bid YTM</th><th data-k="ytm_chg_bp">Δ bp</th>
      <th data-k="mod">Mod dur</th><th data-k="stale">Last move</th>
    </tr></thead><tbody></tbody>
  </table></div>
</div>

<footer id="foot"></footer>
</div>

<div id="tip"></div>
<dialog id="dlg"><header><h3 id="dTitle"></h3>
  <button class="x" onclick="dlg.close()">Close</button></header>
  <div class="body"><div class="kv" id="dKv"></div>
  <svg id="dChart" viewBox="0 0 700 220" role="img" aria-label="Yield history"></svg>
  <p style="color:var(--muted);font-size:11px;margin-top:14px" id="dNote"></p></div>
</dialog>

<script>
const DATA = /*__DATA__*/;
const B = DATA.bonds, META = DATA.meta;
const STALE_DAYS = 20;
const $ = s => document.querySelector(s);
const fmt = (v,d=2) => v===null||v===undefined||Number.isNaN(v) ? "—" : Number(v).toFixed(d);
const isStale = b => b.stale === null || b.stale === undefined || b.stale >= STALE_DAYS;

/* Which side of the quote drives every yield on the page. Bid is the default
   because it is what AMX shows, but the two are not interchangeable: you sell
   into the bid and buy at the ask, so a buyer's real yield is the ask. */
const basis = () => ($("#fBasis") && $("#fBasis").value) || "bid";
const Y = b => { const v = b["ytm_"+basis()]; return v===undefined ? null : v; };
const PX = b => { const m={bid:"bid",ask:"ask",mid:"price",last:"close"}[basis()];
                  const v=b[m]; return v===undefined ? null : v; };

$("#stamp").textContent = META.latest
  ? `${META.n_days} day${META.n_days===1?"":"s"} recorded · page built ${META.generated}`
  : "No data recorded yet";

/* A shared page that quietly goes stale is worse than one that is obviously
   broken: if the scheduled job fails, Pages keeps serving the last good build
   and nobody notices. So the age of the data is stated in plain language, and
   the badge changes colour once it is older than a normal weekend gap. */
(function(){
  const el=$("#fresh"), txt=$("#freshText");
  if(!META.latest){ el.className="freshness bad-age";
    txt.innerHTML="<b>No data yet</b> — the collector has not run"; return; }
  const d=new Date(META.latest+"T00:00:00");
  const now=new Date(); now.setHours(0,0,0,0);
  const days=Math.round((now-d)/86400000);
  const when = days<=0 ? "today" : days===1 ? "yesterday" : days+" days ago";
  if(days<=1){ el.className="freshness";
    txt.innerHTML=`Data from <b>${META.latest}</b> — updated ${when}`; }
  else if(days<=4){ el.className="freshness";
    txt.innerHTML=`Data from <b>${META.latest}</b> — ${when}, normal across a weekend`; }
  else if(days<=9){ el.className="freshness warn-age";
    txt.innerHTML=`Data from <b>${META.latest}</b> — <b>${when}</b>. The daily update may have failed.`; }
  else { el.className="freshness bad-age";
    txt.innerHTML=`Data from <b>${META.latest}</b> — <b>${when}</b>. This page is stale; check the collector.`; }
})();

/* ---- honest headline: how much of this market actually printed a price ---- */
const withYtm = B.filter(b => b.ytm !== null);
const fresh = B.filter(b => !isStale(b));
$("#lede").innerHTML = B.length
  ? `Tracking <b>${B.length}</b> listed instruments. <b>${fresh.length}</b> moved on price
     within the last ${STALE_DAYS} days — the rest are carrying an old print, and any
     yield shown against them is a number, not an opportunity.`
  : "Run fetch.py to record the first snapshot.";

if (META.gaps && META.gaps.length)
  $("#gapwarn").innerHTML = `<div class="warn"><b>Incomplete instruments:</b>
    ${META.gaps.join("; ")}. These appear with blanks rather than estimates — the
    exchange does not publish the missing detail for them.</div>`;

/* ---- summary, recomputed against the current filters ---- */
function summary(rows){
  const fresh = rows.filter(b=>!isStale(b));
  const fy = fresh.map(b=>Y(b)).filter(v=>v!==null);
  const med = a => { if(!a.length) return null; const s=[...a].sort((x,y)=>x-y);
    const m=Math.floor(s.length/2); return s.length%2?s[m]:(s[m-1]+s[m])/2; };
  const spreads = rows.map(b=>b.spread_bp).filter(v=>v!==null&&v!==undefined);
  const ccy = $("#fCcy").value || "all currencies";
  const cards = [
    ["Instruments", rows.length===B.length ? rows.length : rows.length+" of "+B.length,
     rows.filter(b=>Y(b)!==null).length+" with a yield"],
    ["Median YTM", fy.length?fmt(med(fy))+"%":"—", "Liquid names only"],
    ["Yield range", fy.length?fmt(Math.min(...fy),1)+"–"+fmt(Math.max(...fy),1)+"%":"—","Liquid names only"],
    ["Median bid-ask", spreads.length?fmt(med(spreads),0)+" bp":"—","Round-trip cost"],
    ["Stale marks", rows.length-fresh.length, "No move in "+STALE_DAYS+"+ days"],
  ];
  $("#cards").innerHTML = cards.map(function(c){
    return '<div class="card"><div class="k">'+c[0]+'</div><div class="v">'+c[1]+
           '</div><div class="n">'+c[2]+'</div></div>';}).join("");
  const filtered = rows.length !== B.length;
  const scope = filtered
    ? "<b>"+rows.length+"</b> of the <b>"+B.length+"</b> listed instruments ("+ccy+")"
    : "all <b>"+B.length+"</b> listed instruments";
  const noYield = rows.length - rows.filter(b=>Y(b)!==null).length;
  $("#lede").innerHTML = rows.length
    ? "Showing "+scope+". <b>"+fresh.length+"</b> moved on price within the last "+
      STALE_DAYS+" days — the rest carry an old mark, and any yield shown against them "+
      "is a number, not an opportunity." +
      (noYield ? " <b>"+noYield+"</b> have no computable yield; open one to see why." : "")
    : "Nothing matches these filters.";
}

/* ---- filters ---- */
/* Currency defaults to AMD. Putting a 5% USD bond and a 14% AMD bond on one
   yield axis compares instruments priced off different risk-free curves, so the
   chart shows a single currency unless you deliberately choose otherwise. */
const ccyCounts = {};
B.forEach(b => ccyCounts[b.ccy] = (ccyCounts[b.ccy]||0)+1);
const ccys = Object.keys(ccyCounts).sort((a,b)=>ccyCounts[b]-ccyCounts[a]);
ccys.forEach(c => $("#fCcy").add(new Option(c+" ("+ccyCounts[c]+")", c)));
$("#fCcy").add(new Option("All currencies — mixed curves",""));
$("#fCcy").value = ccys.indexOf("AMD")>=0 ? "AMD" : (ccys[0] || "");

const uniq = k => [...new Set(B.map(b=>b[k]).filter(Boolean))].sort();
uniq("issuer_class").forEach(v=>$("#fClass").add(new Option(v,v)));
uniq("type").forEach(t=>$("#fType").add(new Option(t,t)));

let sortK="ytm", sortDir=-1;
function view(){
  const q=$("#q").value.toLowerCase(), t=$("#fType").value, c=$("#fCcy").value,
        cl=$("#fClass").value, m=$("#fMat").value, hs=$("#hideStale").checked;
  return B.filter(b=>{
    if(q && !(`${b.isin} ${b.issuer} ${b.ticker||""}`.toLowerCase().includes(q))) return false;
    if(t && b.type!==t) return false;
    if(c && b.ccy!==c) return false;
    if(cl && b.issuer_class!==cl) return false;
    if(hs && isStale(b)) return false;
    if(m){ const [lo,hi]=m.split("-").map(Number);
      if(b.yrs===null||b.yrs<lo||b.yrs>=hi) return false; }
    return true;
  }).sort((a,b)=>{
    const x=sortK==="ytm"?Y(a):a[sortK], y=sortK==="ytm"?Y(b):b[sortK];
    if(x===null||x===undefined) return 1;
    if(y===null||y===undefined) return -1;
    return (x>y?1:x<y?-1:0)*sortDir;
  });
}

/* ---- scatter: staleness is the visual grammar ---- */
function scatter(rows){
  const svg=$("#scatter"), W=900,H=420,P={t:18,r:22,b:46,l:56};
  const pts=rows.filter(b=>Y(b)!==null && b.yrs!==null);
  if(!pts.length){ svg.innerHTML=`<text x="450" y="200" text-anchor="middle" class="ax">
    No priced instruments match these filters</text>`; return; }
  const xs=pts.map(p=>p.yrs), ys=pts.map(p=>Y(p));
  const x0=0, x1=Math.max(...xs)*1.05||1;
  const y0=Math.min(...ys)*0.92, y1=Math.max(...ys)*1.05;
  const vols=pts.map(p=>p.outstanding_amd||p.vol||0), vmax=Math.max(...vols,1);
  const X=v=>P.l+(v-x0)/(x1-x0)*(W-P.l-P.r);
  const YS=v=>H-P.b-(v-y0)/((y1-y0)||1)*(H-P.t-P.b);
  let s=`<g class="grid">`;
  for(let i=0;i<=5;i++){const yy=P.t+i*(H-P.t-P.b)/5;
    s+=`<line x1="${P.l}" x2="${W-P.r}" y1="${yy}" y2="${yy}"/>`;}
  s+=`</g>`;
  for(let i=0;i<=5;i++){const v=y1-i*(y1-y0)/5, yy=P.t+i*(H-P.t-P.b)/5;
    s+=`<text class="ax" x="${P.l-9}" y="${yy+3}" text-anchor="end">${v.toFixed(1)}%</text>`;}
  for(let i=0;i<=6;i++){const v=x0+i*(x1-x0)/6;
    s+=`<text class="ax" x="${X(v)}" y="${H-P.b+18}" text-anchor="middle">${v.toFixed(1)}</text>`;}
  s+=`<text class="axlab" x="${(W+P.l)/2}" y="${H-6}" text-anchor="middle">Years to maturity</text>`;
  s+=`<text class="axlab" transform="translate(14,${H/2}) rotate(-90)" text-anchor="middle">${basis()} yield to maturity</text>`;
  pts.forEach((p,i)=>{
    const r=3.5+Math.sqrt((p.outstanding_amd||p.vol||0)/vmax)*7, st=isStale(p);
    s+=`<circle class="dot" data-i="${i}" cx="${X(p.yrs)}" cy="${YS(Y(p))}" r="${r.toFixed(1)}"
      fill="${st?"none":"var(--apricot-soft)"}" stroke="${st?"var(--stale)":"var(--apricot)"}"
      stroke-width="${st?1.5:1}" fill-opacity="${st?0:.82}"/>`;
  });
  svg.innerHTML=s;
  svg.querySelectorAll(".dot").forEach(el=>{
    const p=pts[+el.dataset.i];
    el.onmouseenter=e=>{const t=$("#tip");
      t.innerHTML=`<b>${p.issuer}</b><br>${p.isin}<br>YTM ${fmt(Y(p))}% ·
        ${fmt(p.yrs,1)}y · ${p.ccy}<br>${isStale(p)?"Stale price":"Priced recently"}`;
      t.style.opacity=1;};
    el.onmousemove=e=>{const t=$("#tip");
      t.style.left=Math.min(e.clientX+14,innerWidth-290)+"px";
      t.style.top=(e.clientY+14)+"px";};
    el.onmouseleave=()=>$("#tip").style.opacity=0;
    el.onclick=()=>open_(p);
  });
}

/* ---- table ---- */
let tableOpen = false;
function syncDisclosure(n){
  const btn=$("#toggleTable");
  btn.setAttribute("aria-expanded", tableOpen ? "true" : "false");
  btn.querySelector(".lbl").textContent =
    tableOpen ? "Hide instruments" : (n===1 ? "Show 1 instrument" : `Show ${n} instruments`);
  $("#tableWrap").hidden = !tableOpen;
}

function table(rows){
  $("#count").textContent=`${rows.length} of ${B.length} match`;
  if(!tableOpen) return;   // don't paint rows while collapsed
  $("#tbl tbody").innerHTML=rows.map((b,i)=>{
    const st=isStale(b);
    const chg=b.ytm_chg_bp;
    const cls=chg===null?"":chg>0?"up":chg<0?"dn":"";
    const pct=b.stale===null?0:Math.max(0,1-Math.min(b.stale,60)/60);
    return `<tr tabindex="0" data-i="${i}" class="${st?"is-stale":""}">
      <td class="l">${b.isin}</td><td class="l">${b.issuer}</td>
      <td class="l"><span class="tag">${b.issuer_class||"—"}</span></td>
      <td class="l"><span class="tag">${b.type}</span></td><td>${b.ccy}</td>
      <td>${fmt(b.coupon,2)}${b.coupon!==null?"%":""}</td><td>${b.maturity||"—"}</td>
      <td>${fmt(b.yrs,1)}</td><td>${fmt(PX(b),2)}</td>
      <td>${b.spread_bp===null||b.spread_bp===undefined?"—":fmt(b.spread_bp,0)}</td>
      <td><b>${fmt(Y(b),2)}${Y(b)!==null?"%":""}</b></td>
      <td class="${cls}">${chg===null?"—":(chg>0?"+":"")+fmt(chg,0)}</td>
      <td>${fmt(b.mod,2)}</td>
      <td><span class="freshbar"><i style="width:${(pct*100).toFixed(0)}%"></i></span>
        <span style="margin-left:6px;color:var(--faint)">${b.stale===null?"—":b.stale+"d"}</span></td>
    </tr>`;}).join("");
  $("#tbl tbody").querySelectorAll("tr").forEach(tr=>{
    const b=rows[+tr.dataset.i];
    tr.onclick=()=>open_(b);
    tr.onkeydown=e=>{if(e.key==="Enter") open_(b);};
  });
}

/* ---- detail ---- */
function open_(b){
  $("#dTitle").textContent=`${b.issuer} · ${b.isin}`;
  $("#dKv").innerHTML=[
    ["Mark",fmt(b.price,3)],["Source",b.price_source||"—"],
    ["Bid",fmt(b.bid,3)],["Ask",fmt(b.ask,3)],
    ["Bid-ask",b.spread_bp===null||b.spread_bp===undefined?"—":fmt(b.spread_bp,0)+" bp"],
    ["Bid yield",b.ytm_bid===null||b.ytm_bid===undefined?"—":fmt(b.ytm_bid)+"%"],
    ["Ask yield",b.ytm_ask===null||b.ytm_ask===undefined?"—":fmt(b.ytm_ask)+"%"],
    ["Mid yield",b.ytm_mid===null||b.ytm_mid===undefined?"—":fmt(b.ytm_mid)+"%"],
    ["AMX bid yield",b.amx_ytm_bid===null||b.amx_ytm_bid===undefined?"—":fmt(b.amx_ytm_bid)+"%"],
    ["YTM (effective)",b.ytm_effective===null||b.ytm_effective===undefined?"—":fmt(b.ytm_effective)+"%"],
    ["AMX yield",b.amx_ytm===null||b.amx_ytm===undefined?"—":fmt(b.amx_ytm)+"%"],
    ["Difference",b.ytm_diff_bp===null||b.ytm_diff_bp===undefined?"—":fmt(b.ytm_diff_bp,1)+" bp"],
    ["Issuer type",b.issuer_class||"—"],["Coupon",b.coupon!==null?fmt(b.coupon)+"%":"—"],
    ["Maturity",b.maturity||"—"],["Years",fmt(b.yrs,2)],
    ["Modified duration",fmt(b.mod,3)],["Convexity",fmt(b.conv,2)],
    ["Accrued",fmt(b.accrued,3)],["Currency",b.ccy],
    ["Days since move",b.stale===null?"—":b.stale+"d"],["Type",b.type],
  ].map(([k,v])=>`<div><span>${k}</span>${v}</div>`).join("");
  $("#dNote").textContent = b.note
    ? `Not computed: ${b.note}.`
    : (isStale(b)
      ? `This price has not moved in ${b.stale===null?"the whole record":b.stale+" days"}. Treat the yield as indicative only.`
      : "");
  spark(b.ts);
  $("#dlg").showModal();
}
function spark(ts){
  const svg=$("#dChart"),W=700,H=220,P={t:14,r:14,b:28,l:48};
  const pts=(ts||[]).filter(p=>p.y!==null);
  if(pts.length<2){ svg.innerHTML=`<text x="350" y="110" text-anchor="middle" class="ax">
    Not enough history yet — one point per trading day</text>`; return; }
  const ys=pts.map(p=>p.y), y0=Math.min(...ys)*.98, y1=Math.max(...ys)*1.02;
  const X=i=>P.l+i/(pts.length-1)*(W-P.l-P.r);
  const Y=v=>H-P.b-(v-y0)/((y1-y0)||1)*(H-P.t-P.b);
  let s=`<g class="grid">`;
  for(let i=0;i<=3;i++){const yy=P.t+i*(H-P.t-P.b)/3;
    s+=`<line x1="${P.l}" x2="${W-P.r}" y1="${yy}" y2="${yy}"/>`;}
  s+=`</g>`;
  for(let i=0;i<=3;i++){const v=y1-i*(y1-y0)/3,yy=P.t+i*(H-P.t-P.b)/3;
    s+=`<text class="ax" x="${P.l-8}" y="${yy+3}" text-anchor="end">${v.toFixed(2)}%</text>`;}
  s+=`<path d="${pts.map((p,i)=>`${i?"L":"M"}${X(i)},${Y(p.y)}`).join("")}"
      fill="none" stroke="var(--apricot)" stroke-width="1.8"/>`;
  s+=`<text class="ax" x="${P.l}" y="${H-8}">${pts[0].d}</text>`;
  s+=`<text class="ax" x="${W-P.r}" y="${H-8}" text-anchor="end">${pts[pts.length-1].d}</text>`;
  svg.innerHTML=s;
}

/* ---- wiring ---- */
function refresh(){
  const lbl={bid:"Bid",ask:"Ask",mid:"Mid",last:"Last"}[basis()];
  const th=$("#thYtm"); if(th) th.textContent = lbl+" YTM";
  const v=view(); summary(v); scatter(v); syncDisclosure(v.length); table(v);
}

$("#toggleTable").addEventListener("click", ()=>{
  tableOpen = !tableOpen;
  const v = view();
  syncDisclosure(v.length);
  table(v);
  if(tableOpen) $("#tableWrap").scrollIntoView({behavior:"smooth", block:"start"});
});
["#q","#fBasis","#fCcy","#fClass","#fType","#fMat","#hideStale"].forEach(s=>{
  $(s).addEventListener("input",refresh); $(s).addEventListener("change",refresh);});
document.querySelectorAll("th[data-k]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k; sortDir = sortK===k ? -sortDir : -1; sortK=k; refresh();});
$("#foot").innerHTML = `Source: AMX market data API · history kept in data/history.csv ·
  a bond is marked stale when its close price has not changed for ${STALE_DAYS} days.<br>
  Analysis only, not investment advice. Yields assume the stated coupon frequency and
  ACT/365; verify against the prospectus before trading.`;
refresh();
</script>
</body></html>
"""


if __name__ == "__main__":
    build()
