"""The council dashboard page (served at ``/dashboard``). One file, no
external assets: the engine may run where nothing else is reachable.

Markdown (questions, answers, the roles' messages) is rendered by the
small renderer in the page, which escapes everything before it adds any
markup. JSON (typed records, tool arguments, event payloads) is shown as
labelled fields, never raw."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Council Dashboard</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1b1f24;--muted:#5b6470;--line:#e2e5ea;
--accent:#2f6fde;--ok:#1f8a4c;--warn:#b7791f;--bad:#c53030;--chip:#eef1f5;--code:#f1f3f6;
--r-planner:#7c4dff;--r-researcher:#2f6fde;--r-tool_executor:#0f8f8f;--r-critic:#d9730d;
--r-meta_critic:#b7791f;--r-coder:#1f8a4c;--r-synthesizer:#c2185b;--r-adversary:#c53030;--r-user:#5b6470;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1216;--panel:#161b21;
--ink:#e6e9ee;--muted:#9aa4b2;--line:#262d36;--accent:#6aa0ff;--chip:#1e252e;--code:#1b2129}}
:root[data-theme="dark"]{--bg:#0f1216;--panel:#161b21;--ink:#e6e9ee;--muted:#9aa4b2;--line:#262d36;--accent:#6aa0ff;--chip:#1e252e;--code:#1b2129}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);
background:var(--panel);position:sticky;top:0;z-index:3;flex-wrap:wrap}
h1{font-size:16px;margin:0 8px 0 0}h2{font-size:15px;margin:20px 0 8px}
.badge{padding:2px 8px;border-radius:999px;background:var(--chip);font-size:12px}
input,select,button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:4px 8px}button{cursor:pointer}button:hover{border-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(300px,32%) 1fr;height:calc(100vh - 50px)}
#list{overflow:auto;border-right:1px solid var(--line)}#detail{overflow:auto;padding:12px 20px 60px}
@media (max-width:900px){main{grid-template-columns:1fr;height:auto}#list{max-height:40vh}#detail{padding:12px 16px 60px}}
.row{padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--chip)}.row.sel{background:var(--chip);box-shadow:inset 3px 0 var(--accent)}
.row .q{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{color:var(--muted);font-size:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.st{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.st.running,.st.paused,.st.awaiting_adversary,.st.suspended{color:var(--accent)}
.st.completed{color:var(--ok)}.st.failed,.st.lost{color:var(--bad)}.st.cancelled{color:var(--warn)}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:14px 0 4px;position:sticky;top:0;background:var(--bg);z-index:2}
.tabs button{border:none;border-bottom:2px solid transparent;border-radius:0;background:none;padding:8px 14px;color:var(--muted)}
.tabs button.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;background:var(--panel)}th.n,td.n{text-align:right;font-variant-numeric:tabular-nums}
.tablewrap{overflow:auto;max-width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chip{background:var(--chip);border-radius:6px;padding:1px 8px;font-size:12px;white-space:nowrap}
.chip.in_progress{outline:1px solid var(--accent);color:var(--accent)}.chip.done{color:var(--ok)}
.chip.bad{color:var(--bad)}.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}
pre,code{font-family:var(--mono);font-size:12.5px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--code);border:1px solid var(--line);border-radius:8px;padding:10px;margin:6px 0}
.ok{color:var(--ok)}.bad{color:var(--bad)}.muted{color:var(--muted)}
details{margin:4px 0}summary{cursor:pointer;color:var(--muted)}
.empty{color:var(--muted);padding:30px 16px}
.fold{position:relative;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 14px}
.fold>.fold-body{max-height:14.5em;overflow:hidden}.fold.open>.fold-body{max-height:none}
.fold.long:not(.open)>.fold-body{-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}
.flap{position:absolute;left:50%;bottom:-11px;transform:translateX(-50%);padding:0 14px;height:20px;line-height:18px;
border-radius:0 0 8px 8px;font-size:12px;cursor:pointer;background:var(--chip);border:1px solid var(--line)}
.md{overflow-wrap:anywhere}.md>:first-child{margin-top:0}.md>:last-child{margin-bottom:0}
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{margin:14px 0 6px;line-height:1.3}
.md h1{font-size:18px}.md h2{font-size:16px}.md h3{font-size:15px}.md h4,.md h5,.md h6{font-size:14px}
.md p{margin:6px 0}.md ul,.md ol{margin:6px 0;padding-left:22px}.md li{margin:2px 0}
.md code{background:var(--code);padding:1px 4px;border-radius:4px}.md pre code{background:none;padding:0}
.md blockquote{margin:6px 0;padding:2px 12px;border-left:3px solid var(--line);color:var(--muted)}
.md table{margin:8px 0;display:block;overflow:auto}.md th,.md td{overflow-wrap:normal;word-break:normal;min-width:4em}.md hr{border:none;border-top:1px solid var(--line);margin:12px 0}
.md a{color:var(--accent)}
.clip{max-height:340px;overflow:hidden;position:relative}
.clip::after{content:"";position:absolute;left:0;right:0;bottom:0;height:60px;background:linear-gradient(transparent,var(--panel))}
.more{margin-top:4px;font-size:12px}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:6px 0}
dl.kv>dt{color:var(--muted);font-size:12.5px;padding-top:1px}dl.kv>dd{margin:0;min-width:0}
ol.kvl{margin:4px 0;padding-left:20px}
.flow{display:flex;flex-direction:column;gap:10px;margin-top:10px}
.msg{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--rc,var(--accent));border-radius:8px;padding:10px 14px}
.msg .hd{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.who{font-weight:600;color:var(--rc,var(--ink))}.arrow{color:var(--muted)}
.to{font-weight:600}.to span{color:var(--c)}
.verdict{font-weight:600;border-radius:6px;padding:1px 8px;font-size:12px}
.verdict.ready{background:rgba(31,138,76,.14);color:var(--ok)}.verdict.needs_more_research{background:rgba(183,121,31,.16);color:var(--warn)}
.typed{border:1px dashed var(--line);border-radius:6px;padding:6px 10px;margin:6px 0}
.sig{display:flex;gap:8px;align-items:flex-start;font-size:13px;color:var(--muted);padding:2px 6px 2px 10px;border-left:2px dotted var(--line);margin-left:10px}
.sig .ic{width:18px;flex:none;text-align:center}.sig .tx{flex:1;min-width:0}.sig.inject,.sig.rewind,.sig.awaiting_adversary{color:var(--warn)}
.sig.guard,.sig.truncation{color:var(--bad)}.sig.council_complete{color:var(--ok)}
.btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.btns button,.btns select{font-size:12px;padding:2px 8px}
.given{margin-top:8px;border-top:1px dashed var(--line);padding-top:6px}
.filters{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--muted)}
#drawer{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;z-index:10}
#drawer.on{display:block}
#drawer .panel{position:absolute;top:0;right:0;bottom:0;width:min(980px,100%);background:var(--bg);overflow:auto;padding:14px 18px 60px;border-left:1px solid var(--line)}
.cm{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0}
.cm .rl{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.cm.assistant{border-left:3px solid var(--accent)}.cm.tool{border-left:3px solid var(--line)}
.cm.harness{border-left:3px solid var(--warn)}.cm.resp{outline:2px solid var(--accent)}
.call{font-family:var(--mono);font-size:12.5px;margin:4px 0}.call b{color:var(--accent)}
.ev{font-family:var(--mono);font-size:12px;padding:2px 0;border-bottom:1px dashed var(--line)}
.ev .k{font-weight:600}
</style>
</head>
<body>
<header>
  <h1>Councils</h1>
  <span class="badge" id="running">…</span>
  <input id="q" placeholder="search question or sid" size="24">
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
<div id="drawer"><div class="panel" id="drawer-body"></div></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LIVE=["running","paused","awaiting_tool","awaiting_adversary","suspended","queued"];
const ICON={inject:"↪",rewind:"⟲",awaiting_adversary:"⚑",adversary_ack:"✔",decision:"◆",guard:"⚠",truncation:"✂",
  lane_retry:"↻",lane_escalation:"⇧",council_complete:"✓",wrap_up:"⏹",cancel:"✕",infra_wait:"⏳",pause:"⏸",resume:"▶",checkpoint:"●"};
let sel=null,tab="overview",es=null,lastId=0,listTimer=null,detTimer=null,flowKey=null,openState={},D=null,FLOW=[];
let callAtLoad=null;
{const h=location.hash.slice(1).split("/");sel=h[0]||null;tab=h[1]||"overview";
 const m=(h[2]||"").match(/^call=(\d+)$/);if(m)callAtLoad=+m[1]}
const fmtT=t=>t?new Date(t*1000).toLocaleString():"";
const fmtClock=t=>t?new Date(t*1000).toLocaleTimeString():"";
const fmtD=s=>{if(s==null)return"";s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return(h?h+"h ":"")+(m||h?m+"m ":"")+x+"s"};
const num=n=>n==null?"":Number(n).toLocaleString();
const base=p=>(p||"").split("/").filter(Boolean).pop()||p;
const human=k=>String(k).replace(/_/g," ");
const rc=r=>`--rc:var(--r-${String(r||"").split(":")[0]},var(--accent))`;
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error(r.status+" "+await r.text());return r.json()}

/* markdown: everything is escaped first, then markup is added */
function inline(t){
  const codes=[];t=esc(t).replace(/`([^`\n]+)`/g,(_,c)=>{codes.push(c);return"\u0000"+(codes.length-1)+"\u0000"});
  t=t.replace(/\$\\(rightarrow|to)\$/g,"→").replace(/\$\\leftarrow\$/g,"←").replace(/\$\\times\$/g,"×").replace(/\$\\triangle\$/g,"△")
   .replace(/\*\*([^*\n]+)\*\*/g,"<strong>$1</strong>").replace(/__([^_\n]+)__/g,"<strong>$1</strong>")
   .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g,"$1<em>$2</em>").replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g,"$1<em>$2</em>")
   .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s"]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t.replace(/\u0000(\d+)\u0000/g,(_,i)=>"<code>"+codes[+i]+"</code>");
}
const ITEM=/^\s*([-*+]|\d{1,3}[.)])\s+/;
/* a JSON object or array in a message: shown as labelled fields */
function asJson(t){t=t.trim();if(!/^[\[{]/.test(t))return undefined;
  try{const v=JSON.parse(t);return v&&typeof v==="object"?v:undefined}catch(_){return undefined}}
function jsonBlock(v,raw){return`<div class="typed">${kv(v)}<details><summary>raw JSON</summary><pre>${esc(raw)}</pre></details></div>`}
// A panel that shows a long text folded, with a flap to open it fully
// (the command output panel of Cerebriline). Open panels stay open
// across the live refresh.
const openFolds=new Set();
function fold(key,html){return `<div class="fold${openFolds.has(key)?" open":""}" data-fold="${esc(key)}"><div class="fold-body">${html}</div><button class="flap" hidden title="show all">▾</button></div>`}
function armFolds(root){(root||document).querySelectorAll(".fold").forEach(f=>{
  const b=f.querySelector(".fold-body"),k=f.dataset.fold,flap=f.querySelector(".flap");
  const long=f.classList.contains("open")||b.scrollHeight>b.clientHeight+4;
  f.classList.toggle("long",long);flap.hidden=!long;flap.textContent=f.classList.contains("open")?"▴":"▾";
  flap.onclick=()=>{const o=f.classList.toggle("open");o?openFolds.add(k):openFolds.delete(k);
    flap.textContent=o?"▴":"▾";flap.title=o?"fold":"show all"}})}
function md(src){
  const L=String(src??"").replace(/\r\n?/g,"\n").split("\n");const out=[];let i=0,para=[];
  const flush=()=>{if(para.length){out.push("<p>"+para.map(inline).join("<br>")+"</p>");para=[]}};
  const isTable=k=>/^\s*\|.*\|\s*$/.test(L[k]||"")&&/^\s*\|?\s*:?-{2,}/.test(L[k+1]||"");
  const cells=s=>{s=s.trim().replace(/^\|/,"").replace(/(^|[^\\])\|$/,"$1");const out=[];let cur="",tick=false;
    for(let k=0;k<s.length;k++){const ch=s[k];
      if(ch==="\\"&&s[k+1]==="|"){cur+="|";k++;continue}       /* \| is a literal bar */
      if(ch==="`")tick=!tick;
      if(ch==="|"&&!tick){out.push(cur.trim());cur="";continue}
      cur+=ch}
    out.push(cur.trim());return out};
  while(i<L.length){
    const l=L[i];let m;
    if((m=l.match(/^\s*(```|~~~)/))){flush();const f=m[1];const buf=[];i++;
      while(i<L.length&&!L[i].trim().startsWith(f))buf.push(L[i++]);i++;
      const body=buf.join("\n"),js=asJson(body);
      out.push(js!==undefined?jsonBlock(js,body):`<pre><code>${esc(body)}</code></pre>`);continue}
    if(/^\s*[\[{]/.test(l)){let k=i,buf=[];while(k<L.length&&L[k].trim())buf.push(L[k++]);
      const js=asJson(buf.join("\n"));if(js!==undefined){flush();out.push(jsonBlock(js,buf.join("\n")));i=k;continue}}
    if(!l.trim()){flush();i++;continue}
    if((m=l.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))){flush();const n=m[1].length;out.push(`<h${n}>${inline(m[2])}</h${n}>`);i++;continue}
    if(/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(l)){flush();out.push("<hr>");i++;continue}
    if(isTable(i)){flush();const head=cells(L[i]);i+=2;const rows=[];
      while(i<L.length&&/^\s*\|/.test(L[i]))rows.push(cells(L[i++]));
      out.push("<table><tr>"+head.map(c=>`<th>${inline(c)}</th>`).join("")+"</tr>"+
        rows.map(r=>"<tr>"+r.map(c=>`<td>${inline(c)}</td>`).join("")+"</tr>").join("")+"</table>");continue}
    if(/^\s{0,3}>/.test(l)){flush();const buf=[];while(i<L.length&&/^\s{0,3}>/.test(L[i]))buf.push(L[i++].replace(/^\s{0,3}>\s?/,""));
      out.push("<blockquote>"+md(buf.join("\n"))+"</blockquote>");continue}
    if(ITEM.test(l)){flush();const buf=[];
      while(i<L.length&&(ITEM.test(L[i])||(L[i].trim()&&/^\s{2,}/.test(L[i]))||(!L[i].trim()&&(ITEM.test(L[i+1]||"")||/^\s{2,}\S/.test(L[i+1]||"")))))buf.push(L[i++]);
      out.push(mdList(buf));continue}
    para.push(l.trim());i++;
  }
  flush();return out.join("");
}
function mdList(lines){
  const ind=s=>s.match(/^\s*/)[0].length;const top=Math.min(...lines.filter(s=>ITEM.test(s)).map(ind));
  const groups=[];   /* a bullet list and a numbered one are two lists */
  for(const s of lines){const m=s.match(/^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/);
    if(m&&m[1].length===top){const ord=/\d/.test(m[2]);
      if(!groups.length||groups.at(-1).ord!==ord)groups.push({ord,items:[]});groups.at(-1).items.push([m[3]])}
    else if(groups.length)groups.at(-1).items.at(-1).push(s.slice(Math.min(ind(s),top+2)))}
  return groups.map(g=>{const tag=g.ord?"ol":"ul";
    return`<${tag}>`+g.items.map(([h,...rest])=>{const sub=rest.join("\n").trim();return"<li>"+inline(h)+(sub?md(sub):"")+"</li>"}).join("")+`</${tag}>`}).join("");
}
/* JSON as labelled fields */
function kv(v){
  if(v==null||v==="")return'<span class="muted">—</span>';
  if(typeof v==="boolean")return v?"yes":"no";
  if(typeof v==="number")return esc(v.toLocaleString());
  if(typeof v==="string"){if(/^\s*[\[{]/.test(v)){try{return kv(JSON.parse(v))}catch(_){}}
    return v.length>140||v.includes("\n")?`<div class="md">${md(v)}</div>`:esc(v)}
  if(Array.isArray(v)){if(!v.length)return'<span class="muted">none</span>';
    if(v.every(x=>x==null||typeof x!=="object")&&v.join(", ").length<300)return v.map(x=>`<span class="chip">${esc(x)}</span>`).join(" ");
    return'<ol class="kvl">'+v.map(x=>"<li>"+kv(x)+"</li>").join("")+"</ol>"}
  const ks=Object.keys(v);if(!ks.length)return'<span class="muted">none</span>';
  return'<dl class="kv">'+ks.map(k=>`<dt>${esc(human(k))}</dt><dd>${kv(v[k])}</dd>`).join("")+"</dl>";
}
function clip(html,key){const open=openState[key];
  return`<div class="md ${open?"":"clip"}" data-clip="${esc(key)}">${html}</div><button class="more" onclick="toggleClip(this)">${open?"show less":"show all"}</button>`}
function toggleClip(b){const d=b.previousElementSibling;const on=d.classList.toggle("clip");openState[d.dataset.clip]=!on;b.textContent=on?"show all":"show less"}

/* the council list */
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
function setHash(){history.replaceState(null,"","#"+(sel||"")+(tab!=="overview"?"/"+tab:""))}
function select(sid){sel=sid;lastId=0;flowKey=null;openState={};setHash();
  document.querySelectorAll(".row").forEach(r=>r.classList.toggle("sel",r.dataset.sid===sid));loadDetail()}

/* one council */
function lanesTable(lanes){if(!lanes||!lanes.length)return'<p class="muted">No lane records.</p>';
  return`<div class="tablewrap"><table><tr><th>role</th><th>round</th><th>lane</th><th>model</th><th class=n>calls</th>
  <th class=n>tools</th><th class=n>cmds</th><th class=n>prompt tok</th><th class=n>out tok</th><th class=n>span</th><th>ended</th></tr>`+
  lanes.map(l=>`<tr><td style="${rc(l.role)}"><span class="who">${esc(l.role)}</span></td><td>${esc(l.round??"")}</td><td>${esc(l.lane_idx??"")}</td><td>${esc((l.models||[]).join(", "))}</td>
  <td class=n>${num(l.llm_calls)}</td><td class=n>${num(l.tool_calls)}</td><td class=n>${num(l.commands)}</td><td class=n>${num(l.prompt_tokens)}</td>
  <td class=n>${num(l.completion_tokens)}</td><td class=n>${fmtD(l.span_s)}</td>
  <td class="${l.failure_class?"bad":""}">${esc(l.stop_reason||"")}${l.failure_class?" ("+esc(l.failure_class)+")":""}</td></tr>`).join("")+"</table></div>"}
function cmdList(cmds){if(!cmds||!cmds.length)return'<p class="muted">No commands ran.</p>';
  return cmds.map(c=>`<details><summary class="call"><span class="${String(c.exit).startsWith("exit 0")?"ok":"bad"}">${esc(c.exit)}</span>
  ${esc(c.id||"")} [${esc(c.role)} ${esc(c.lane??"")}] ${esc(String(c.command).slice(0,180))}</summary>
  <pre>${esc(c.command)}</pre>${c.printed?`<div class="muted">printed</div><pre>${esc(c.printed)}</pre>`:""}<pre>${esc(c.tail)}</pre></details>`).join("")}
function evLine(e){const d=e.data||{};const who=[e.role,e.round!=null?"r"+e.round:"",e.lane_idx!=null?"#"+e.lane_idx:""].filter(Boolean).join(" ");
  const keys=Object.keys(d).filter(k=>!["kind","role","round","lane_idx","ts"].includes(k));
  return`<div class="ev"><span class="muted">${fmtClock(e.ts)}</span> <span class="k">${esc(e.kind)}</span> ${esc(who)}
   ${keys.length?`<details><summary>${keys.slice(0,6).map(esc).join(", ")}</summary>${kv(Object.fromEntries(keys.map(k=>[k,d[k]])))}</details>`:""}</div>`}
async function loadDetail(){
  if(!sel)return;try{D=await j("/v1/dashboard/councils/"+encodeURIComponent(sel))}catch(e){$("#detail").innerHTML=`<div class="empty bad">${esc(e.message)}</div>`;return}
  const c=D.council||{},m=D.meta||{},live=D.live;const status=live?live.status:(c.status||m.status);
  $("#detail").innerHTML=`<div class="meta"><span class="st ${esc(status)}">${esc(status)}</span><span>${esc(c.sid)}</span><span>${esc(c.effort||m.effort||"")}</span>
   <span>${fmtT(c.started_at||m.started_at)}</span><span>${esc(c.cwd)}</span></div>
   ${live&&live.progress?`<div class="chips">${Object.entries(live.progress).map(([r,s])=>`<span class="chip ${esc(s)}">${esc(r)}: ${esc(s)}</span>`).join("")}
     ${live.phase?`<span class="chip in_progress">${esc(live.phase)}</span>`:""}${(live.flags||[]).map(f=>`<span class="chip">${esc(f)}</span>`).join("")}</div>`:""}
   <div class="tabs">${["overview","messages","timeline"].map(t=>`<button class="${t===tab?"on":""}" data-tab="${t}">${t}</button>`).join("")}</div>
   <div id="tabbody"></div>`;
  document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
  flowKey=null;renderTab();
  if(es){es.close();es=null}
  if(LIVE.includes(status)&&window.EventSource){es=new EventSource("/v1/consult/"+encodeURIComponent(sel)+"/events");
    es.onmessage=onEv;["node_enter","node_exit","inject","rewind","guard","awaiting_adversary","council_complete","lane_retry","truncation"].forEach(k=>es.addEventListener(k,onEv));
    es.onerror=()=>{es&&es.close();es=null}}
}
function setTab(t){tab=t;setHash();document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===t));flowKey=null;renderTab()}
function renderTab(){const B=$("#tabbody");if(!B||!D)return;
  if(tab==="messages"){B.innerHTML=`<div class="filters"><label><input type="checkbox" id="f-sig" checked> control signals</label>
    <label><input type="checkbox" id="f-guard"> guard trips</label><span id="flow-note"></span></div><div class="flow" id="flow"><div class="muted">Loading…</div></div>`;
    $("#f-sig").onchange=$("#f-guard").onchange=()=>{flowKey=null;loadFlow()};loadFlow();return}
  if(tab==="timeline"){B.innerHTML=`<div id="tl">${(D.timeline||[]).map(evLine).join("")||'<p class="muted">No events.</p>'}</div>`;
    lastId=Math.max(0,...(D.timeline||[]).map(e=>e.id));return}
  const c=D.council||{},m=D.meta||{},t=D.totals||{};
  const models=m.models?Object.entries(m.models).map(([r,x])=>`<span class="chip" style="${rc(r)}"><span class="who">${esc(r)}</span>: ${esc(Array.isArray(x)?x.join(", "):x)}</span>`).join(""):"";
  B.innerHTML=`<h2>Question</h2>${fold(`q:${c.sid||sel}`,`<div class="md">${md(m.question&&m.question.length>(c.question||"").length?m.question:(c.question||m.question||""))}</div>`)}
   <div class="chips" style="margin-top:12px">${models}</div>
   <div class="chips"><span class="chip">${num(t.llm_calls)} LLM calls</span><span class="chip">${num(t.prompt_tokens)} prompt tokens</span>
     <span class="chip">${num(t.completion_tokens)} output tokens</span>${c.shell?`<span class="chip">shell ${esc(c.shell.mode||"")}</span>`:""}
     ${(D.chain||[]).length>1?`<span class="chip">${D.chain.length} in consultancy</span>`:""}</div>
   ${D.missing?'<p class="bad">The session directory is gone; only the index row remains.</p>':""}
   <h2>Answer</h2>${D.answer?`<div class="md">${md(D.answer.replace(/^---\n[\s\S]*?\n---\n/,""))}</div>`:'<p class="muted">No answer yet.</p>'}
   <h2>Lanes</h2>${lanesTable(D.lanes)}
   <h2>Commands (${(D.commands||[]).length})</h2>${cmdList(D.commands)}`;
  armFolds(B);
}

/* the messages between the roles */
function toLabel(to){return(to||[]).map(r=>`<span style="--c:var(--r-${esc(r)},var(--ink))">${esc(r)}</span>`).join(", ")}
function msgCard(it,idx){
  const lane=it.lane!=null?` · lane ${it.lane}`:"";
  const stop=it.stop_reason?`<span class="chip ${it.failure_class?"bad":""}">${esc(human(it.stop_reason))}${it.failure_class&&it.failure_class!==it.stop_reason?" ("+esc(human(it.failure_class))+")":""}</span>`:"";
  const body=it.text?clip(md(it.text),"m"+idx+":"+it.ts):(it.typed?"":'<p class="muted">(no message text)</p>');
  const typed=it.typed?`<div class="typed"><div class="muted">typed record <b>${esc(it.typed.record)}</b></div>${kv(it.typed.fields)}</div>`:"";
  const calls=it.calls||[];
  return`<div class="msg" style="${rc(it.from)}"><div class="hd"><span class="who">${esc(it.from)}${esc(lane)}</span>
   <span class="arrow">→</span><span class="to">${toLabel(it.to)}</span>
   ${it.decision?`<span class="verdict ${esc(it.decision)}">${esc(human(it.decision))}</span>`:""}
   ${it.confidence!=null?`<span class="chip">confidence ${esc(it.confidence)}</span>`:""}
   ${it.running?'<span class="chip in_progress">working…</span>':""}${stop}
   <span class="meta">${esc((it.models||[]).join(", "))} · ${fmtClock(it.ts)}${it.end_ts?" · "+fmtD(it.end_ts-it.ts):""} ·
   ${num(calls.length)} calls · ${num(it.tools)} tools · ${num(it.prompt_tokens)} → ${num(it.completion_tokens)} tok</span></div>
   ${typed}${body}
   <div class="btns"><button data-given="${idx}">what it was given (${num(it.given.chars)} chars)</button>
   ${calls.length?`<select data-calls="1"><option value="">open a call…</option>${calls.map((c,k)=>`<option value="${c.id}">call ${k+1}${c.phase?" ("+esc(c.phase)+")":""} · ${esc(c.model||"")}</option>`).join("")}</select>`:""}</div>
   <div class="given" hidden></div></div>`}
function sigLine(it){const keys=Object.keys(it.data||{}).filter(k=>k!=="kind");
  return`<div class="sig ${esc(it.kind)}"><span class="ic">${ICON[it.kind]||"•"}</span><div class="tx">${esc(it.text)} <span class="muted">${fmtClock(it.ts)}</span>
  ${keys.length?`<details><summary>details</summary>${kv(Object.fromEntries(keys.map(k=>[k,it.data[k]])))}</details>`:""}</div></div>`}
async function loadFlow(){const F=$("#flow");if(!F||!sel)return;let d;
  try{d=await j("/v1/dashboard/councils/"+encodeURIComponent(sel)+"/messages")}catch(e){F.innerHTML=`<p class="bad">${esc(e.message)}</p>`;return}
  const sig=$("#f-sig")?.checked,guard=$("#f-guard")?.checked;
  FLOW=d.items||[];
  const shown=FLOW.filter(it=>it.type==="message"||(sig&&(guard||!["guard","truncation"].includes(it.kind))));
  const key=shown.length+":"+shown.filter(x=>x.running).length+":"+shown.map(x=>(x.text||"").length).join(",");
  if(key===flowKey)return;flowKey=key;
  const n=FLOW.filter(x=>x.type==="message").length;$("#flow-note").textContent=`${n} messages between ${(d.roles||[]).join(", ")}`;
  F.innerHTML=shown.length?shown.map(it=>it.type==="message"?msgCard(it,FLOW.indexOf(it)):sigLine(it)).join(""):'<p class="muted">No messages yet.</p>';
  F.querySelectorAll("[data-given]").forEach(b=>b.onclick=()=>toggleGiven(b,+b.dataset.given));
  F.querySelectorAll("select[data-calls]").forEach(s=>s.onchange=()=>{if(s.value)openCall(+s.value);s.value=""})}
function toggleGiven(b,idx){const box=b.closest(".msg").querySelector(".given");const it=FLOW[idx];
  if(!box.hidden){box.hidden=true;return}
  box.innerHTML=`<div class="muted">Its opening request: ${num(it.given.system_chars)} chars of system prompt, then what it was handed${it.given.chars>it.given.preview.length?" (the start)":""}:</div>
    <div class="md">${md(it.given.preview)}</div><button class="more">open the full request</button>`;
  box.querySelector("button").onclick=()=>openCall(it.given.call);box.hidden=false}
async function openCall(id){const P=$("#drawer-body");$("#drawer").classList.add("on");P.innerHTML='<p class="muted">Loading…</p>';
  let c;try{c=await j("/v1/dashboard/councils/"+encodeURIComponent(sel)+"/calls/"+id)}catch(e){P.innerHTML=`<p class="bad">${esc(e.message)}</p>`;return}
  const one=(m,resp)=>{const role=resp?"answer":m.harness?"harness note":m.role==="tool"?"tool result":m.role;
    const tc=(m.tool_calls||[]).map(t=>`<div class="call">→ <b>${esc(t.name)}</b>${t.args&&typeof t.args==="object"?kv(t.args):" "+esc(t.args??"")}</div>`).join("");
    const content=!m.content?"":m.role==="tool"?`<details ${m.content.length<1500?"open":""}><summary>${m.name?esc(m.name)+" · ":""}${num(m.content.length)} chars</summary><pre>${esc(m.content)}</pre></details>`
      :m.role==="system"?`<details><summary>system prompt · ${num(m.content.length)} chars</summary><div class="md">${md(m.content)}</div></details>`:`<div class="md">${md(m.content)}</div>`;
    const think=m.thinking?`<details><summary>thinking · ${num(m.thinking.length)} chars</summary><pre>${esc(m.thinking)}</pre></details>`:"";
    return`<div class="cm ${esc(m.role)} ${m.harness?"harness":""} ${resp?"resp":""}"><div class="rl">${esc(role)}</div>${think}${content}${tc}</div>`};
  P.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
    <div style="${rc(c.role)}"><span class="who">${esc(c.role)}${c.lane!=null?" · lane "+esc(c.lane):""}</span> <span class="muted">${esc(c.model||"")} · ${fmtT(c.ts)} · ${num(c.prompt_tokens)} → ${num(c.completion_tokens)} tok${c.duration_ms?" · "+fmtD(c.duration_ms/1000):""}</span></div>
    <button id="drawer-close">close</button></div>
    ${c.error?`<p class="bad">${esc(c.error)}</p>`:""}
    ${(c.tools_offered||[]).length?`<div class="chips"><span class="muted">tools offered:</span>${c.tools_offered.map(t=>`<span class="chip">${esc(t)}</span>`).join("")}</div>`:""}
    ${Object.keys(c.options||{}).length?kv(c.options):""}
    ${(c.messages||[]).map(m=>one(m,false)).join("")}${c.response?one(c.response,true):'<p class="muted">No answer recorded.</p>'}`;
  $("#drawer-close").onclick=closeDrawer}
function closeDrawer(){$("#drawer").classList.remove("on")}
$("#drawer").onclick=e=>{if(e.target.id==="drawer")closeDrawer()};document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDrawer()});
function onEv(ev){const id=+ev.lastEventId||0;if(id&&id<=lastId)return;lastId=id||lastId;let data={};try{data=JSON.parse(ev.data)}catch(_){}
  if(tab==="timeline"){const tl=$("#tl");if(tl)tl.insertAdjacentHTML("beforeend",evLine({ts:Date.now()/1000,kind:ev.type==="message"?(data.kind||"event"):ev.type,role:data.role,round:data.round,lane_idx:data.lane_idx,data}))}
  if(tab==="messages"&&["node_exit","inject","rewind","awaiting_adversary","council_complete"].includes(ev.type))setTimeout(loadFlow,600);
  if(ev.type==="council_complete")setTimeout(loadDetail,1000)}
function schedule(){clearInterval(listTimer);clearInterval(detTimer);if(!$("#auto").checked)return;
  listTimer=setInterval(loadList,5000);
  detTimer=setInterval(()=>{if(!sel||!D||!(D.live&&LIVE.includes(D.live.status)))return;
    if(tab==="messages")loadFlow();else if(tab==="overview")loadDetail()},10000)}
$("#q").oninput=()=>{clearTimeout(window._qt);window._qt=setTimeout(loadList,300)};$("#status").onchange=loadList;$("#auto").onchange=schedule;
window.onhashchange=()=>{const h=location.hash.slice(1).split("/");if(h[0]&&h[0]!==sel){tab=h[1]||"overview";select(h[0])}};
loadList().then(async()=>{if(sel){await loadDetail();if(callAtLoad)openCall(callAtLoad)}});schedule();
</script>
</body>
</html>
"""
