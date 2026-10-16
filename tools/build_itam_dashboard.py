"""Build a single self-contained, fully offline HTML dashboard from the live ITAM database.

Reuses the portal's own aggregations (portal/app/dashboards.py), so every figure matches the portal.
Chart.js is inlined from the portal's front-end dependencies - no CDN, works with no internet.

    portal\\.venv\\Scripts\\python tools\\build_itam_dashboard.py [out.html]   (default: samples/ITAM_Dashboard.html)
"""
import datetime as dt
import decimal
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))
from app import dashboards, db  # noqa: E402

CHARTJS = ROOT / "portal" / "frontend" / "node_modules" / "chart.js" / "dist" / "chart.umd.js"


def _j(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "ITAM_Dashboard.html"
    db.open_pool()
    # ponytail: one embedded dataset per engineer (+ ALL) so the filter is instant and offline; fine while engineers are few.
    engineers = [r["label"] for r in dashboards.assets()["by_engineer"] if r["label"] != "UNASSIGNED"]
    data = {"ALL": {"assets": dashboards.assets(), "calls": dashboards.calls()}}
    for e in engineers:
        data[e] = {"assets": dashboards.assets(e), "calls": dashboards.calls(e)}
    html = TEMPLATE.replace("/*CHARTJS*/", CHARTJS.read_text(encoding="utf-8").replace("</script", "<\\/script")) \
                   .replace("/*DATA*/null", json.dumps(data, default=_j).replace("</", "<\\/")) \
                   .replace("__BUILT__", dt.datetime.now().strftime("%d %b %Y %H:%M"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    db.close_pool()
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(engineers)} engineers)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ITAM Dashboard - ONGC Ankleshwar</title>
<style>
:root{--bg:#f4f4f4;--card:#fff;--tx:#161616;--tx2:#525252;--line:#e0e0e0;--hd:#161616;
--c1:#0f62fe;--c2:#8a3ffc;--c3:#009d9a;--c4:#ee5396;--c5:#b28600;--c6:#198038;--bad:#da1e28;--ok:#198038;--warn:#b28600}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161616;--card:#262626;--tx:#f4f4f4;--tx2:#c6c6c6;--line:#393939;--hd:#0b0b0b;
--c1:#78a9ff;--c2:#be95ff;--c3:#3ddbd9;--c4:#ff7eb6;--c5:#f1c21b;--c6:#42be65;--bad:#fa4d56;--ok:#42be65;--warn:#f1c21b}}
:root[data-theme=dark]{--bg:#161616;--card:#262626;--tx:#f4f4f4;--tx2:#c6c6c6;--line:#393939;--hd:#0b0b0b;
--c1:#78a9ff;--c2:#be95ff;--c3:#3ddbd9;--c4:#ff7eb6;--c5:#f1c21b;--c6:#42be65;--bad:#fa4d56;--ok:#42be65;--warn:#f1c21b}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--tx);line-height:1.45}
.wrap{max-width:1400px;margin:0 auto;padding:16px}
header{background:var(--hd);color:#f4f4f4;padding:16px 20px;display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center;margin-bottom:16px}
header h1{font-size:18px;font-weight:600}header small{color:#c6c6c6;display:block;font-weight:400;font-size:12px}
.ctl{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
select,button{font:inherit;font-size:13px;padding:6px 10px;border:1px solid #6f6f6f;background:#262626;color:#f4f4f4;cursor:pointer}
button[aria-pressed=true]{background:var(--c1);border-color:var(--c1);color:#fff}
select:focus-visible,button:focus-visible,th:focus-visible{outline:2px solid #fff;outline-offset:2px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);padding:16px}
.kpi .l{font-size:12px;color:var(--tx2);text-transform:uppercase;letter-spacing:.4px}
.kpi .v{font-size:28px;font-weight:600;margin:2px 0}.kpi .s{font-size:12px;color:var(--tx2)}
.kpi.bad .v{color:var(--bad)}.kpi.warn .v{color:var(--warn)}.kpi.ok .v{color:var(--ok)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;margin-bottom:12px}
.card h2{font-size:14px;font-weight:600;margin-bottom:10px}
.ch{position:relative;height:280px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px;border-bottom:2px solid var(--line);color:var(--tx2);font-size:12px;text-transform:uppercase;cursor:pointer;white-space:nowrap}
td{padding:8px;border-bottom:1px solid var(--line)}tr:hover td{background:color-mix(in srgb,var(--line) 40%,transparent)}
.scroll{overflow-x:auto}.num{text-align:right;font-variant-numeric:tabular-nums}
.hide{display:none}footer{color:var(--tx2);font-size:12px;margin-top:8px}
@media print{header .ctl{display:none}.card{break-inside:avoid}}
</style></head><body>
<div class="wrap">
<header><h1>ITAM Dashboard<small>ONGC Ankleshwar - live from the portal database, offline snapshot</small></h1>
<div class="ctl"><label for="eng">Engineer</label><select id="eng"></select>
<span role="group" aria-label="View"><button id="vA" aria-pressed="true">Assets</button> <button id="vC" aria-pressed="false">Service calls</button></span></div></header>
<div id="assets"><section class="kpis" id="aK"></section><section class="grid" id="aG"></section></div>
<div id="calls" class="hide"><section class="kpis" id="cK"></section><section class="grid" id="cG"></section></div>
<div class="card scroll"><h2 id="tT"></h2><table id="tbl"></table></div>
<footer>Built __BUILT__. Data as of <span id="asof"></span>. Re-run <code>tools/build_itam_dashboard.py</code> to refresh.</footer>
</div>
<script>/*CHARTJS*/</script>
<script>
const DATA=/*DATA*/null;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const PAL=()=>[1,2,3,4,5,6].map(i=>css('--c'+i));
const $=id=>document.getElementById(id);
const fmt=v=>v==null?'-':Number(v).toLocaleString('en-IN');
let view='A',charts=[],sortKey=null,sortDir=-1;

function kpi(l,v,s,cls){return `<div class="card kpi ${cls||''}"><div class="l">${l}</div><div class="v">${fmt(v)}</div><div class="s">${s||''}</div></div>`}
function card(id,t){return `<div class="card"><h2>${t}</h2><div class="ch"><canvas id="${id}" role="img" aria-label="${t}"></canvas></div></div>`}
function mk(id,type,labels,sets,o={}){
  const P=PAL(),tx=css('--tx2'),ln=css('--line'),pie=type==='doughnut';
  charts.push(new Chart($(id),{type,data:{labels,datasets:sets.map((s,i)=>({label:s.label,data:s.data,
    backgroundColor:pie?P:(s.bg||P[i%6]),borderColor:s.line||P[i%6],borderWidth:type==='line'?2:0,tension:.3,pointRadius:0,fill:false}))},
    options:{responsive:true,maintainAspectRatio:false,animation:false,indexAxis:o.h?'y':'x',color:tx,
      plugins:{legend:{display:sets.length>1||pie,position:pie?'right':'top',labels:{color:tx,usePointStyle:true}}},
      scales:pie?{}:{x:{stacked:!!o.stack,grid:{color:ln},ticks:{color:tx}},y:{stacked:!!o.stack,beginAtZero:true,grid:{color:ln},ticks:{color:tx}}}}}));
}
const lab=r=>r.map(x=>x.label), col=(r,k)=>r.map(x=>x[k]);

function renderAssets(a){
  const k=a.kpi;
  $('aK').innerHTML=kpi('Assets',k.assets,fmt(k.components)+' components')+kpi('In use',k.in_use,fmt(k.idle)+' idle')
   +kpi('Cover expired',k.cover_expired,fmt(k.cover_expiring)+' expiring in 90 days',k.cover_expired?'bad':'ok')
   +kpi('PM pending',k.pm_pending,fmt(k.pm_done)+' done · '+(k.pm_quarter||''),k.pm_pending?'warn':'ok')
   +kpi('Users inactive',k.users_inactive,'assets held by non-active staff',k.users_inactive?'warn':'ok')
   +kpi('Data-quality rows',k.dq_rows,fmt(a.network.missing_ip)+' missing IP',k.dq_rows?'warn':'ok');
  $('aG').innerHTML=card('a1','Assets by class (status split)')+card('a2','Warranty / AMC cover')+card('a3','Cover expiring - next 12 months')
   +card('a4','Preventive maintenance by class')+card('a5','Top locations')+card('a6','Top makes')+card('a7','Install year')+card('a8','Data-quality flags');
  mk('a1','bar',lab(a.by_class),[{label:'In use',data:col(a.by_class,'in_use')},{label:'Idle',data:col(a.by_class,'idle')},{label:'Other',data:col(a.by_class,'other')}],{stack:1,h:1});
  mk('a2','doughnut',lab(a.cover),[{data:col(a.cover,'n')}]);
  mk('a3','bar',lab(a.expiry),[{label:'Assets',data:col(a.expiry,'n')}]);
  mk('a4','bar',lab(a.pm),[{label:'Done',data:col(a.pm,'done')},{label:'Done outside quarter',data:col(a.pm,'stale')},{label:'Pending',data:col(a.pm,'pending')}],{stack:1});
  mk('a5','bar',lab(a.locations),[{label:'Assets',data:col(a.locations,'n')}],{h:1});
  mk('a6','bar',lab(a.by_make),[{label:'Assets',data:col(a.by_make,'n')}],{h:1});
  mk('a7','bar',lab(a.age),[{label:'Assets',data:col(a.age,'n')}]);
  mk('a8','bar',lab(a.flags),[{label:'Rows',data:col(a.flags,'n')}],{h:1});
  $('asof').textContent=k.as_of||'';
  table('Engineer workload - assets',[['label','Engineer'],['total','Assets'],['cover_risk','Cover at risk'],['pm_pending','PM pending']],a.by_engineer);
}
function renderCalls(c){
  const k=c.kpi,s=c.sla,pct=s.closed?Math.round(100*s.within_3d/s.closed):0;
  $('cK').innerHTML=kpi('Open calls',k.open,fmt(k.total)+' total',k.open?'warn':'ok')+kpi('Awaiting part',k.awaiting_part,'spare pending')
   +kpi('Oldest open (days)',k.oldest_open,'median '+fmt(Math.round(k.median_open_age||0)),k.oldest_open>60?'bad':'')
   +kpi('Avg turnaround (days)',k.avg_tat,'closed within 3 days: '+pct+'%')+kpi('This month',k.this_month,fmt(k.last_month)+' last month')
   +kpi('Repeat-fault assets',k.repeat_assets,'3+ calls',k.repeat_assets?'warn':'ok');
  $('cG').innerHTML=card('c1','Calls per month (closed vs open)')+card('c2','Open-call ageing')+card('c3','Last 30 days: opened vs closed')
   +card('c4','Top problems')+card('c5','Calls by asset class')+card('c6','Priority')+card('c7','Calls by site')+card('c8','Spare parts most requested');
  mk('c1','bar',lab(c.monthly),[{label:'Closed',data:col(c.monthly,'closed')},{label:'Open',data:col(c.monthly,'open')}],{stack:1});
  mk('c2','bar',lab(c.ageing),[{label:'Open calls',data:col(c.ageing,'n')}]);
  mk('c3','line',c.daily.map(d=>d.day.slice(5)),[{label:'Opened',data:col(c.daily,'opened')},{label:'Closed',data:col(c.daily,'closed')}]);
  mk('c4','bar',lab(c.problems),[{label:'Calls',data:col(c.problems,'n')}],{h:1});
  mk('c5','doughnut',lab(c.by_class),[{data:col(c.by_class,'n')}]);
  mk('c6','doughnut',lab(c.priority),[{data:col(c.priority,'n')}]);
  mk('c7','bar',lab(c.by_site),[{label:'Calls',data:col(c.by_site,'n')}],{h:1});
  mk('c8','bar',lab(c.spares.top_parts),[{label:'Parts',data:col(c.spares.top_parts,'n')}],{h:1});
  $('asof').textContent=k.as_of||'';
  table('Engineer workload - service calls',[['label','Engineer'],['total','Calls'],['open','Open'],['closed','Closed'],['avg_tat','Avg TAT (d)'],['oldest','Oldest open (d)']],c.by_engineer);
}
let tCols,tRows;
function table(title,cols,rows){tCols=cols;tRows=rows;sortKey=null;$('tT').textContent=title;drawTable()}
function drawTable(){
  const r=[...tRows];if(sortKey)r.sort((a,b)=>sortDir*((a[sortKey]??-1)>(b[sortKey]??-1)?1:-1));
  $('tbl').innerHTML='<thead><tr>'+tCols.map(([k,l],i)=>`<th tabindex="0" data-k="${k}" aria-sort="${sortKey===k?(sortDir>0?'ascending':'descending'):'none'}">${l}</th>`).join('')+'</tr></thead><tbody>'
   +r.map(x=>'<tr>'+tCols.map(([k],i)=>`<td class="${i?'num':''}">${i?fmt(x[k]):String(x[k]).replace(/</g,'&lt;')}</td>`).join('')+'</tr>').join('')+'</tbody>';
  $('tbl').querySelectorAll('th').forEach(th=>{const go=()=>{sortDir=sortKey===th.dataset.k?-sortDir:-1;sortKey=th.dataset.k;drawTable()};th.onclick=go;th.onkeydown=e=>{if(e.key==='Enter')go()}});
}
function render(){
  charts.forEach(c=>c.destroy());charts=[];
  const d=DATA[$('eng').value];
  $('assets').classList.toggle('hide',view!=='A');$('calls').classList.toggle('hide',view!=='C');
  $('vA').setAttribute('aria-pressed',view==='A');$('vC').setAttribute('aria-pressed',view==='C');
  // chart canvases must be visible to size correctly, so only the active view is drawn
  view==='A'?renderAssets(d.assets):renderCalls(d.calls);
}
$('eng').innerHTML=Object.keys(DATA).map(k=>`<option value="${k}">${k==='ALL'?'All engineers':k}</option>`).join('');
$('eng').onchange=render;$('vA').onclick=()=>{view='A';render()};$('vC').onclick=()=>{view='C';render()};
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',render);
render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
