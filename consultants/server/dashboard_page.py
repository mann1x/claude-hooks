"""The council dashboard page (served at ``/dashboard``). One file, no
external assets: the engine may run where nothing else is reachable."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Council Dashboard</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--muted:#5b6470;--line:#e2e5ea;
--accent:#2f6fde;--ok:#1f8a4c;--warn:#b7791f;--bad:#c53030;--run:#2f6fde;--chip:#eef1f5;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1216;--panel:#161b21;
--ink:#e6e9ee;--muted:#9aa4b2;--line:#262d36;--accent:#6aa0ff;--chip:#1e252e}}
:root[data-theme="dark"]{--bg:#0f1216;--panel:#161b21;--ink:#e6e9ee;--muted:#9aa4b2;--line:#262d36;--accent:#6aa0ff;--chip:#1e252e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);
background:var(--panel);position:sticky;top:0;z-index:2;flex-wrap:wrap}
h1{font-size:16px;margin:0 8px 0 0}h2{font-size:15px;margin:18px 0 8px}
.badge{padding:2px 8px;border-radius:999px;background:var(--chip);font-size:12px}
input,select,button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:5px 8px}button{cursor:pointer}
main{display:grid;grid-template-columns:minmax(320px,38%) 1fr;gap:0;height:calc(100vh - 54px)}
#list{overflow:auto;border-right:1px solid var(--line)}#detail{overflow:auto;padding:12px 18px 40px}
@media (max-width:900px){main{grid-template-columns:1fr;height:auto}#list{max-height:45vh}}
.row{padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--chip)}.row.sel{background:var(--chip);box-shadow:inset 3px 0 var(--accent)}
.row .q{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
.meta{color:var(--muted);font-size:12px;display:flex;gap:10px;flex-wrap:wrap}
.st{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.st.running,.st.paused,.st.awaiting_adversary,.st.suspended{color:var(--run)}
.st.completed{color:var(--ok)}.st.failed,.st.lost{color:var(--bad)}.st.cancelled{color:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.tablewrap{overflow:auto;max-width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chip{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:12px}
.chip.in_progress{outline:1px solid var(--run);color:var(--run)}.chip.done{color:var(--ok)}
pre{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:12.5px;
background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;margin:0}
.cmd{font-family:var(--mono);font-size:12px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.muted{color:var(--muted)}details{margin:6px 0}summary{cursor:pointer;color:var(--muted)}
.ev{font-family:var(--mono);font-size:12px;padding:2px 0;border-bottom:1px dashed var(--line)}
.ev .k{font-weight:600}.ev.rewind .k,.ev.inject .k,.ev.awaiting_adversary .k{color:var(--warn)}
.ev.guard .k{color:var(--bad)}.ev.checkpoint .k{color:var(--accent)}
.empty{color:var(--muted);padding:30px 16px}
</style>
</head>
<body>
<header>
  <h1>Councils</h1>
  <span class="badge" id="running">…</span>
  <input id="q" placeholder="search question or sid" size="26">
  <select id="status"><option value="">all statuses</option><option>running</option>
    <option>completed</option><option>failed</option><option>cancelled</option>
    <option>suspended</option><option>lost</option></select>
  <label class="muted"><input type="checkbox" id="auto" checked> auto-refresh</label>
  <span class="muted" id="updated"></span>
</header>
<main>
  <section id="list"><div class="empty">Loading…</div></section>
  <section id="detail"><div class="empty">Select a council.</div></section>
</main>
<script>
const $=s=>document.querySelector(s);const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const LIVE=["running","paused","awaiting_tool","awaiting_adversary","suspended","queued"];
let sel=location.hash.slice(1)||null, es=null, lastId=0, listTimer=null, detTimer=null;
const fmtT=t=>t?new Date(t*1000).toLocaleString():"";
const fmtD=s=>{if(s==null)return"";s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return(h?h+"h ":"")+(m||h?m+"m ":"")+x+"s"};
const num=n=>n==null?"":Number(n).toLocaleString();
const base=p=>(p||"").split("/").filter(Boolean).pop()||p;
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error(r.status+" "+await r.text());return r.json()}
async function loadList(){
  const p=new URLSearchParams({limit:300});if($("#q").value)p.set("q",$("#q").value);if($("#status").value)p.set("status",$("#status").value);
  try{const d=await j("/v1/dashboard/councils?"+p);
    $("#running").textContent=d.running+" running";$("#updated").textContent="updated "+new Date().toLocaleTimeString();
    const L=$("#list");if(!d.councils.length){L.innerHTML='<div class="empty">No councils match.</div>';return}
    L.innerHTML=d.councils.map(c=>`<div class="row ${c.sid===sel?"sel":""}" data-sid="${esc(c.sid)}">
      <div class="meta"><span class="st ${esc(c.status)}">${esc(c.status||"?")}</span><span>${esc(base(c.cwd))}</span>
      <span>${esc(c.effort||"")}</span>${c.kind==="follow-up"?"<span>follow-up</span>":""}<span>${fmtT(c.started_at)}</span>
      <span>${fmtD(c.duration_s)}</span>${c.missing?"<span class=bad>missing</span>":""}</div>
      <div class="q">${esc((c.question||"").replace(/\s+/g," ").slice(0,220))}</div>
      ${c.live&&c.live.progress?`<div class="chips">${Object.entries(c.live.progress).map(([r,s])=>`<span class="chip ${esc(s)}">${esc(r)}</span>`).join("")}</div>`:""}
    </div>`).join("");
    L.querySelectorAll(".row").forEach(e=>e.onclick=()=>select(e.dataset.sid));
  }catch(e){$("#list").innerHTML=`<div class="empty bad">${esc(e.message)}</div>`}
}
function select(sid){sel=sid;location.hash=sid;lastId=0;document.querySelectorAll(".row").forEach(r=>r.classList.toggle("sel",r.dataset.sid===sid));loadDetail(true)}
function lanesTable(lanes){if(!lanes||!lanes.length)return'<p class="muted">No lane records.</p>';
  return`<div class="tablewrap"><table><tr><th>role</th><th>round</th><th>lane</th><th>model</th><th class=n>calls</th>
  <th class=n>tools</th><th class=n>cmds</th><th class=n>prompt tok</th><th class=n>out tok</th><th class=n>span</th><th>ended</th></tr>`+
  lanes.map(l=>`<tr><td>${esc(l.role)}</td><td>${esc(l.round??"")}</td><td>${esc(l.lane_idx??"")}</td><td>${esc((l.models||[]).join(", "))}</td>
  <td class=n>${num(l.llm_calls)}</td><td class=n>${num(l.tool_calls)}</td><td class=n>${num(l.commands)}</td><td class=n>${num(l.prompt_tokens)}</td>
  <td class=n>${num(l.completion_tokens)}</td><td class=n>${fmtD(l.span_s)}</td>
  <td class="${l.failure_class?"bad":""}">${esc(l.stop_reason||"")}${l.failure_class?" ("+esc(l.failure_class)+")":""}</td></tr>`).join("")+"</table></div>"}
function cmdList(cmds){if(!cmds||!cmds.length)return'<p class="muted">No commands ran.</p>';
  return cmds.map(c=>`<details><summary class="cmd"><span class="${c.exit.startsWith("exit 0")?"ok":"bad"}">${esc(c.exit)}</span>
  [${esc(c.role)} ${esc(c.lane??"")}] ${esc(c.command.slice(0,180))}</summary><pre>${esc(c.command)}\n\n${esc(c.tail)}</pre></details>`).join("")}
function evLine(e){const d=e.data||{};const who=[e.role,e.round!=null?"r"+e.round:"",e.lane_idx!=null?"#"+e.lane_idx:""].filter(Boolean).join(" ");
  const bits=Object.entries(d).filter(([k])=>!["kind","role","round","lane_idx","ts"].includes(k)).slice(0,6).map(([k,v])=>k+"="+(typeof v==="object"?JSON.stringify(v).slice(0,120):String(v).slice(0,120))).join(" ");
  return`<div class="ev ${esc(e.kind)}"><span class="muted">${new Date(e.ts*1000).toLocaleTimeString()}</span> <span class="k">${esc(e.kind)}</span> ${esc(who)} <span class="muted">${esc(bits)}</span></div>`}
async function loadDetail(first){
  if(!sel)return;let d;try{d=await j("/v1/dashboard/councils/"+encodeURIComponent(sel))}catch(e){$("#detail").innerHTML=`<div class="empty bad">${esc(e.message)}</div>`;return}
  const c=d.council||{},m=d.meta||{},live=d.live,t=d.totals||{};const status=live?live.status:(c.status||m.status);
  const models=m.models?Object.entries(m.models).map(([r,x])=>`<span class="chip">${esc(r)}: ${esc(Array.isArray(x)?x.join(", "):x)}</span>`).join(""):"";
  $("#detail").innerHTML=`<div class="meta"><span class="st ${esc(status)}">${esc(status)}</span><span>${esc(c.sid)}</span><span>${esc(c.effort||m.effort||"")}</span>
   <span>${fmtT(c.started_at||m.started_at)}</span><span>${esc(c.cwd)}</span></div>
   <h2>Question</h2><pre>${esc(c.question||m.question||"")}</pre>
   ${live&&live.progress?`<h2>Progress</h2><div class="chips">${Object.entries(live.progress).map(([r,s])=>`<span class="chip ${esc(s)}">${esc(r)}: ${esc(s)}</span>`).join("")}
     ${live.phase?`<span class="chip in_progress">${esc(live.phase)}</span>`:""}${(live.flags||[]).map(f=>`<span class="chip">${esc(f)}</span>`).join("")}</div>`:""}
   <div class="chips">${models}</div>
   <div class="chips"><span class="chip">${num(t.llm_calls)} LLM calls</span><span class="chip">${num(t.prompt_tokens)} prompt tokens</span>
     <span class="chip">${num(t.completion_tokens)} output tokens</span>${c.shell?`<span class="chip">shell ${esc(c.shell.mode||"")}</span>`:""}
     ${(d.chain||[]).length>1?`<span class="chip">${d.chain.length} in consultancy</span>`:""}</div>
   ${d.missing?'<p class="bad">The session directory is gone; only the index row remains.</p>':""}
   <h2>Lanes</h2>${lanesTable(d.lanes)}
   <h2>Commands (${(d.commands||[]).length})</h2>${cmdList(d.commands)}
   <h2>Answer</h2>${d.answer?`<pre>${esc(d.answer)}</pre>`:'<p class="muted">No answer yet.</p>'}
   <h2>Timeline</h2><div id="tl">${(d.timeline||[]).map(evLine).join("")}</div>`;
  lastId=Math.max(0,...(d.timeline||[]).map(e=>e.id));
  if(es){es.close();es=null}
  if(LIVE.includes(status)&&window.EventSource){es=new EventSource("/v1/consult/"+encodeURIComponent(sel)+"/events");
    es.onmessage=ev=>appendEv(ev);["node_enter","node_exit","inject","rewind","guard","checkpoint","awaiting_adversary","council_complete","decision","lane_retry"].forEach(k=>es.addEventListener(k,appendEv));
    es.onerror=()=>{es&&es.close();es=null}}
}
function appendEv(ev){const id=+ev.lastEventId||0;if(id&&id<=lastId)return;lastId=id||lastId;let data={};try{data=JSON.parse(ev.data)}catch(_){}
  if(["llm_call","tool_call","stall.attempt.ok","tool_approval_auto"].includes(ev.type))return;
  const tl=$("#tl");if(tl)tl.insertAdjacentHTML("beforeend",evLine({ts:Date.now()/1000,kind:ev.type==="message"?(data.kind||"event"):ev.type,role:data.role,round:data.round,lane_idx:data.lane_idx,data}));
  if(ev.type==="council_complete")setTimeout(()=>loadDetail(),800)}
function schedule(){clearInterval(listTimer);clearInterval(detTimer);if(!$("#auto").checked)return;
  listTimer=setInterval(loadList,5000);detTimer=setInterval(()=>{if(sel)loadDetail()},15000)}
$("#q").oninput=()=>{clearTimeout(window._qt);window._qt=setTimeout(loadList,300)};$("#status").onchange=loadList;$("#auto").onchange=schedule;
window.onhashchange=()=>{const h=location.hash.slice(1);if(h&&h!==sel)select(h)};
loadList().then(()=>{if(sel)loadDetail(true)});schedule();
</script>
</body>
</html>
"""
