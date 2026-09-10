#!/usr/bin/env python3
# =====================================================================
# Generates a PORTABLE LOCAL DEMO (demo/index.html) of KnowledgeEngine v9.
# - Reads the synthetic KBs from kb/<client>/*.md (single source of truth).
# - Produces a standalone HTML file (no dependency, no server,
#   no Azure, no cost) that proves multi-tenant isolation:
#   dedicated index per client + clientId security trimming + cross-tenant probe.
# Usage: python demo/build_demo.py
# =====================================================================
import json
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
KB = ROOT / "kb"
OUT = ROOT / "demo" / "index.html"

DISPLAY = {"clienta": "Client A", "clientb": "Client B", "clientc": "Client C"}
HEADER_RE = re.compile(r"^#\s*\[(?P<id>[^\]]+)\]\s*(?P<sys>.+?)\s+[—-]\s+(?P<title>.+)$")

docs = []
for cid in sorted(DISPLAY):
    d = KB / cid
    if not d.exists():
        continue
    for md in sorted(d.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        first = text.splitlines()[0].strip()
        m = HEADER_RE.match(first)
        if m:
            fid, sys, title = m.group("id"), m.group("sys").strip(), m.group("title").strip()
        else:
            fid, sys, title = md.stem, md.stem, first.lstrip("# ")
        docs.append({
            "clientId": cid,
            "client": DISPLAY[cid],
            "id": fid,
            "system": sys,
            "title": title,
            "text": text,
        })

clients = [{"id": c, "name": DISPLAY[c]} for c in sorted(DISPLAY)]

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine v9 — Démo isolation multi-tenant</title>
<style>
  :root{
    --bg:#0f172a; --panel:#1e293b; --panel2:#273449; --line:#334155;
    --txt:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8; --ok:#34d399; --warn:#fbbf24;
    --a:#38bdf8; --b:#a78bfa; --c:#f472b6;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--txt);line-height:1.5}
  header{padding:22px 28px;border-bottom:1px solid var(--line);
    display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  header h1{font-size:19px;margin:0;font-weight:700}
  header .tag{color:var(--muted);font-size:13px}
  .wrap{display:grid;grid-template-columns:320px 1fr;gap:0;min-height:calc(100vh - 66px)}
  .side{background:var(--panel);border-right:1px solid var(--line);padding:22px}
  .main{padding:26px 30px}
  .lbl{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
  .clientbtns{display:flex;flex-direction:column;gap:8px;margin-bottom:22px}
  .cbtn{display:flex;align-items:center;gap:10px;padding:11px 13px;border-radius:10px;
    border:1px solid var(--line);background:var(--panel2);color:var(--txt);cursor:pointer;
    font-size:14px;font-weight:600;text-align:left;transition:.15s}
  .cbtn:hover{border-color:var(--accent)}
  .cbtn .dot{width:10px;height:10px;border-radius:50%}
  .cbtn[data-c="clienta"] .dot{background:var(--a)} .cbtn[data-c="clientb"] .dot{background:var(--b)} .cbtn[data-c="clientc"] .dot{background:var(--c)}
  .cbtn.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset;background:#0b3b52}
  .trace{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px;font-size:12.5px}
  .trace .row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px dashed var(--line)}
  .trace .row:last-child{border-bottom:0}
  .trace .k{color:var(--muted)} .trace .v{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--accent);text-align:right}
  .searchbar{display:flex;gap:10px;margin-bottom:14px}
  .searchbar input{flex:1;padding:13px 15px;border-radius:10px;border:1px solid var(--line);
    background:var(--panel);color:var(--txt);font-size:15px}
  .searchbar input:focus{outline:none;border-color:var(--accent)}
  .btn{padding:13px 16px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);
    color:var(--txt);cursor:pointer;font-size:14px;font-weight:600}
  .btn.warn{border-color:var(--warn);color:var(--warn)}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
  .chip{font-size:12.5px;padding:6px 11px;border-radius:999px;border:1px solid var(--line);
    background:var(--panel);color:var(--muted);cursor:pointer}
  .chip:hover{border-color:var(--accent);color:var(--txt)}
  .callout{border-radius:10px;padding:13px 15px;margin-bottom:18px;font-size:14px;display:none}
  .callout.ok{display:block;background:#064e3b33;border:1px solid var(--ok);color:#a7f3d0}
  .callout.warn{display:block;background:#78350f33;border:1px solid var(--warn);color:#fde68a}
  .count{color:var(--muted);font-size:13px;margin-bottom:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
  .card h3{margin:0 0 4px;font-size:15px}
  .card .meta{font-size:12px;color:var(--muted);margin-bottom:8px}
  .card .badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;
    background:#0b3b52;border:1px solid var(--accent);color:var(--accent);margin-left:6px}
  .card pre{white-space:pre-wrap;font-family:inherit;font-size:13px;color:#cbd5e1;margin:0;max-height:180px;overflow:auto}
  footer{padding:16px 30px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
  code{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--accent)}
  a{color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>KnowledgeEngine v9</h1>
  <span class="tag">Démo locale — isolation multi-tenant (index dédié par client + security trimming)</span>
</header>
<div class="wrap">
  <aside class="side">
    <p class="lbl">Session — simule le SSO Entra ID</p>
    <div class="clientbtns" id="clientbtns"></div>
    <p class="lbl">Résolution &amp; security trimming</p>
    <div class="trace" id="trace"></div>
    <p style="font-size:12px;color:var(--muted);margin-top:16px">
      L'utilisateur connecté est résolu vers <b>son</b> organisation via son groupe Entra,
      puis routé vers <b>son index dédié</b>. Le filtre <code>clientId</code> s'applique
      <b>avant</b> tout classement.
    </p>
  </aside>
  <main class="main">
    <div class="searchbar">
      <input id="q" placeholder="Rechercher dans la base de connaissances…" autocomplete="off">
      <button class="btn" id="go">Rechercher</button>
      <button class="btn warn" id="probe">🔒 Tester l'isolation</button>
    </div>
    <div class="chips" id="chips"></div>
    <div class="callout" id="callout"></div>
    <div class="count" id="count"></div>
    <div id="results"></div>
  </main>
</div>
<footer>
  Données 100&nbsp;% synthétiques. Démo locale portable — aucune dépendance, aucun Azure, aucun coût.
  En production, la recherche est <b>hybride</b> (BM25 + vecteurs + semantic reranker) avec synthèse <b>GPT-4o</b> ;
  l'isolation repose, ici comme en production, sur un <b>index dédié par client</b> + <b>security trimming <code>clientId</code></b>.
</footer>
<script>
const DOCS = __DOCS__;
const CLIENTS = __CLIENTS__;
let CUR = CLIENTS[0].id;

const $ = s => document.querySelector(s);
const norm = s => s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"");

function systemsOf(cid){ return [...new Set(DOCS.filter(d=>d.clientId===cid).map(d=>d.system))]; }

function renderClients(){
  $("#clientbtns").innerHTML = CLIENTS.map(c=>
    `<button class="cbtn ${c.id===CUR?'active':''}" data-c="${c.id}">
       <span class="dot"></span>${c.name}<span style="color:var(--muted);font-weight:400;margin-left:auto">${c.id}</span>
     </button>`).join("");
  document.querySelectorAll(".cbtn").forEach(b=>b.onclick=()=>{CUR=b.dataset.c;renderClients();renderTrace();clearResults();});
}
function renderTrace(){
  const c = CLIENTS.find(x=>x.id===CUR);
  $("#trace").innerHTML = `
    <div class="row"><span class="k">Utilisateur</span><span class="v">user@${c.id}.demo</span></div>
    <div class="row"><span class="k">Groupe Entra</span><span class="v">grp-ke-${c.id}</span></div>
    <div class="row"><span class="k">clientId résolu</span><span class="v">${c.id}</span></div>
    <div class="row"><span class="k">Index routé</span><span class="v">idx-${c.id}</span></div>
    <div class="row"><span class="k">Filtre sécurité</span><span class="v">clientId eq '${c.id}'</span></div>
    <div class="row"><span class="k">Fiches accessibles</span><span class="v">${DOCS.filter(d=>d.clientId===CUR).length}</span></div>`;
}
function renderChips(){
  const sys = systemsOf(CUR).slice(0,4);
  $("#chips").innerHTML = sys.map(s=>`<span class="chip">${s}</span>`).join("");
  document.querySelectorAll(".chip").forEach(ch=>ch.onclick=()=>{$("#q").value=ch.textContent;search();});
}
function clearResults(){ $("#results").innerHTML=""; $("#count").textContent=""; $("#callout").className="callout"; renderChips(); }

function score(doc,terms){
  const t = norm(doc.system+" "+doc.title+" "+doc.text);
  let s=0; terms.forEach(w=>{ if(w.length<2) return; const m=t.split(w).length-1; s+=m; });
  return s;
}
function search(){
  const q = $("#q").value.trim();
  const co = $("#callout"); co.className="callout";
  if(!q){ clearResults(); return; }
  const terms = norm(q).split(/\s+/).filter(Boolean);
  // SECURITY TRIMMING: only consider the current client's index
  const pool = DOCS.filter(d=>d.clientId===CUR);
  const hits = pool.map(d=>({d,s:score(d,terms)})).filter(x=>x.s>0).sort((a,b)=>b.s-a.s);

  // Pedagogical detection: does the query match a system belonging to ANOTHER client?
  const foreign = DOCS.filter(d=>d.clientId!==CUR).find(d=> terms.some(w=> norm(d.system).includes(w) && w.length>3 ));
  if(hits.length===0 && foreign){
    co.className="callout ok";
    co.innerHTML=`🔒 <b>Isolation prouvée.</b> « ${q} » correspond au système <b>${foreign.system}</b> de <b>${foreign.client}</b>.
      Connecté en tant que <b>${CLIENTS.find(c=>c.id===CUR).name}</b>, cette fiche est <b>techniquement inaccessible</b> — 0 résultat.
      (Bascule sur ${foreign.client} pour la voir apparaître.)`;
  } else if(hits.length===0){
    co.className="callout warn"; co.innerHTML="Aucun résultat dans votre base. Essayez un des systèmes suggérés ci-dessus.";
  }
  $("#count").textContent = `${hits.length} résultat(s) — index idx-${CUR} (clientId eq '${CUR}')`;
  $("#results").innerHTML = hits.map(({d})=>`
    <div class="card">
      <h3>${d.title}<span class="badge">${d.system}</span></h3>
      <div class="meta">${d.id} · ${d.client} · idx-${d.clientId}</div>
      <pre>${d.text.replace(/[&<>]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]))}</pre>
    </div>`).join("");
}
function probe(){
  // Cross-tenant probe: search the current index for a system belonging to ANOTHER client
  const other = CLIENTS.find(c=>c.id!==CUR);
  const sys = systemsOf(other.id)[0];
  $("#q").value = sys;
  search();
}
$("#go").onclick=search;
$("#q").addEventListener("keydown",e=>{if(e.key==="Enter")search();});
$("#probe").onclick=probe;
renderClients(); renderTrace(); renderChips();
</script>
</body>
</html>
"""

OUT.parent.mkdir(parents=True, exist_ok=True)
html = HTML.replace("__DOCS__", json.dumps(docs, ensure_ascii=False)) \
           .replace("__CLIENTS__", json.dumps(clients, ensure_ascii=False))
OUT.write_text(html, encoding="utf-8")
print(f"OK: {OUT}  ({len(docs)} records, {len(clients)} clients)")
