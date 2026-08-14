"""Génère le HTML du diagramme d'architecture PreCogn/VPS en temps réel."""

import os
import sqlite3
import subprocess
from datetime import datetime, timezone

SUBS_DB = "/home/ubuntu/subscriptions_api/subscriptions.db"
LEDGER_ORGS_DIR = "/home/ubuntu/ledger_api/orgs"

SERVICES = [
    ("Coeur comptable", 8080, "ledger_api/app.py"),
    ("subscriptions_api", 8082, "subscriptions_api/app.py"),
    ("analyzor", 8000, "analyzor/main.py"),
    ("llmprecogn", 8001, "llmprecogn"),
]


def _run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def _services():
    out = []
    for name, port, pattern in SERVICES:
        pid = _run(f"pgrep -f '{pattern}' | head -1")
        out.append((name, port, bool(pid)))
    return out


def _orgs():
    try:
        conn = sqlite3.connect(SUBS_DB)
        rows = conn.execute("SELECT org_id, name FROM orgs").fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _ledger_count():
    try:
        return len(os.listdir(LEDGER_ORGS_DIR))
    except Exception:
        return 0


def _disk():
    return _run("df -h / | tail -1 | awk '{print $3\"/\"$2\" (\"$5\")\"}' ")


def generate_archi_html():
    services = _services()
    orgs = _orgs()
    ledger_count = _ledger_count()
    disk = _disk()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    svc_status = {s[0]: s[2] for s in services}

    orgs_html = ""
    # PreCogn au sommet, Structory fille, clients séparés
    ecosystem = {r[0]: r[1] for r in orgs if r[0] not in ("smcspl",)}
    clients = {r[0]: r[1] for r in orgs if r[0] in ("smcspl", "copro_1crE1G2RerFeXQfHNh0yERfvfAjVKGUz53LE9szCqMMs")}
    products = {k: v for k, v in ecosystem.items() if k not in clients}

    for org_id, name in products.items():
        is_parent = org_id == "precogn"
        is_child = org_id == "structory"
        color = "var(--orange)" if is_parent else ("var(--blue)" if is_child else "var(--dim)")
        prefix = "◈ " if is_parent else ("└─ " if is_child else "")
        orgs_html += f'<div class="prod-card"><div class="prod-name" style="color:{color}">{prefix}{name}</div><div class="prod-id">{org_id}</div></div>\n'

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Architecture PreCogn — {now}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0B0C0E; --bg2: #111318; --bg3: #1A1C22;
    --orange: #FF6B00; --orange2: #3A1800;
    --text: #F0E0D0; --dim: #8A6A50; --dim2: #2A1E10;
    --green: #3CB97A; --blue: #5B8FD4; --purple: #9B7EC8; --yellow: #D4A017;
    --border: #2A1E10;
  }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Courier New', monospace;
    font-size: 13px; padding: 20px; }}
  h1 {{ color: var(--orange); font-size: 11px; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 3px; }}
  .ts {{ color: var(--dim); font-size: 9px; margin-bottom: 24px; }}

  .layer {{ border: 1px solid var(--border); border-radius: 3px; margin-bottom: 10px; overflow: hidden; }}
  .lh {{ padding: 5px 12px; font-size: 9px; letter-spacing: 2px; text-transform: uppercase;
    display: flex; align-items: center; gap: 7px; }}
  .dot {{ width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }}
  .lb {{ padding: 10px 12px 12px; display: flex; flex-wrap: wrap; gap: 8px; }}

  .layer.precogn .lh {{ background: #130A00; color: var(--orange); }}
  .layer.precogn .dot {{ background: var(--orange); }}
  .layer.input   .lh {{ background: #0A1020; color: var(--blue); }}
  .layer.input   .dot {{ background: var(--blue); }}
  .layer.gas     .lh {{ background: #0A1A0A; color: var(--green); }}
  .layer.gas     .dot {{ background: var(--green); }}
  .layer.vps     .lh {{ background: #130A00; color: var(--yellow); }}
  .layer.vps     .dot {{ background: var(--yellow); }}
  .layer.data    .lh {{ background: #120A1A; color: var(--purple); }}
  .layer.data    .dot {{ background: var(--purple); }}
  .layer.output  .lh {{ background: #0A1510; color: var(--green); }}
  .layer.output  .dot {{ background: var(--green); }}

  .card {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 2px;
    padding: 8px 11px; min-width: 140px; flex: 1; }}
  .card-title {{ font-size: 11px; font-weight: bold; margin-bottom: 4px; }}
  .card-sub {{ color: var(--dim); font-size: 9px; line-height: 1.6; }}
  .tag {{ display: inline-block; font-size: 8px; padding: 1px 5px; border-radius: 2px;
    margin-top: 4px; margin-right: 2px; }}
  .tag.o {{ background: var(--orange2); color: var(--orange); }}
  .tag.g {{ background: #0A2A1A; color: var(--green); }}
  .tag.b {{ background: #0A1A2A; color: var(--blue); }}
  .tag.p {{ background: #1A0A2A; color: var(--purple); }}
  .tag.y {{ background: #2A1A00; color: var(--yellow); }}
  .tag.x {{ background: var(--bg2); color: var(--dim); }}

  .arrow {{ text-align: center; color: var(--dim2); font-size: 14px; margin: -2px 0; }}

  .orgs {{ margin-top: 20px; border-top: 1px solid var(--border); padding-top: 16px; }}
  .orgs h2 {{ font-size: 9px; letter-spacing: 2px; text-transform: uppercase; color: var(--dim); margin-bottom: 10px; }}
  .org-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr)); gap: 8px; }}
  .prod-card {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 2px; padding: 9px 11px; }}
  .prod-name {{ font-size: 11px; font-weight: bold; margin-bottom: 3px; }}
  .prod-id {{ color: var(--dim); font-size: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  .footer {{ margin-top: 18px; color: var(--dim2); font-size: 8px;
    border-top: 1px solid var(--border); padding-top: 8px; }}

  @media (prefers-color-scheme: light) {{
    :root {{ --bg:#F5F0EB; --bg2:#EDE8E3; --bg3:#E5E0DB; --text:#1A0E00; --border:#D0C0B0; --dim2:#C0A890; --orange2:#FFE0CC; }}
  }}
  :root[data-theme="dark"]  {{ --bg:#0B0C0E; --bg2:#111318; --bg3:#1A1C22; --text:#F0E0D0; --border:#2A1E10; --orange2:#3A1800; --dim2:#2A1E10; }}
  :root[data-theme="light"] {{ --bg:#F5F0EB; --bg2:#EDE8E3; --bg3:#E5E0DB; --text:#1A0E00; --border:#D0C0B0; --orange2:#FFE0CC; --dim2:#C0A890; }}
</style>
</head>
<body>

<h1>◈ PreCogn — Architecture</h1>
<div class="ts">VPS OVH · Google Apps Script · {now} — <a href="/archi" style="color:var(--dim)">actualiser</a></div>

<!-- PRECOGN -->
<div class="layer precogn">
  <div class="lh"><span class="dot"></span>PreCogn — plateforme parente</div>
  <div class="lb">
    <div class="card">
      <div class="card-title" style="color:var(--orange)">◈ PreCogn</div>
      <div class="card-sub">Intelligence décisionnelle · LLM · Executor<br>
        Structory · Compta Copro · Suivre Mes Comptes<br>
        sont des organisations filles ou clientes</div>
    </div>
  </div>
</div>

<div class="arrow">↕</div>

<!-- INPUT -->
<div class="layer input">
  <div class="lh"><span class="dot"></span>Input</div>
  <div class="lb">
    <div class="card">
      <div class="card-title" style="color:var(--blue)">Sheets</div>
      <div class="card-sub">Google Sheets<br>Add-ons liés</div>
      <span class="tag b">saisie</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--blue)">API</div>
      <div class="card-sub">Appels directs<br>vers le VPS</div>
      <span class="tag b">REST</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--blue)">Navigator</div>
      <div class="card-sub">Dashboard universel<br>⊃ Communicator (chat IA)<br>URL publique ?orgId=</div>
      <span class="tag b">web app GAS</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--dim)">Document</div>
      <div class="card-sub">PDF · factures · relevés<br>via Docling</div>
      <span class="tag x">à venir</span>
    </div>
  </div>
</div>

<div class="arrow">↕</div>

<!-- BIBLIOTHEQUE / GAS -->
<div class="layer gas">
  <div class="lh"><span class="dot"></span>Bibliotheque — Google Apps Script</div>
  <div class="lb">
    <div class="card" style="border-color:var(--green)">
      <div class="card-title" style="color:var(--green)">Bibliotheque</div>
      <div class="card-sub">Librairie partagée — tout passe par elle<br>
        ledgerExists() · ledgerQuery() · contexte LLM<br>
        Connecteur VPS pour tous les executors</div>
      <span class="tag g">developmentMode</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--green)">Executors</div>
      <div class="card-sub">
        structory-sheets — compta société<br>
        sheettojournal — saisie → ledger<br>
        sheettocsv — export CSV<br>
        mergesheet · org-onboarding
      </div>
      <span class="tag g">via Bibliotheque</span>
    </div>
  </div>
</div>

<div class="arrow">↕ HTTP :8080 / :8082 / :8000</div>

<!-- VPS -->
<div class="layer vps">
  <div class="lh"><span class="dot"></span>VPS OVH — 213.32.16.118 / vps-03db771f.vps.ovh.net</div>
  <div class="lb">
    <div class="card">
      <div class="card-title" style="color:var(--yellow)">Coeur comptable <span style="color:{'#3CB97A' if svc_status.get('Coeur comptable') else '#E04040'}">●</span></div>
      <div class="card-sub">:8080 · Flask · ledger_api<br>
        /api/ledger/query · provision · fec<br>
        /api/ledger/sheet-entry · journal<br>
        <em style="color:var(--dim)">Moteur ledger-cli</em></div>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--yellow)">subscriptions_api <span style="color:{'#3CB97A' if svc_status.get('subscriptions_api') else '#E04040'}">●</span></div>
      <div class="card-sub">:8082 · Flask<br>
        Stripe TEST · orgs · users<br>
        /welcome · /api/checkout<br>
        <span style="color:#E0A000">⚠ pas de live sans confirmation</span></div>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--yellow)">analyzor <span style="color:{'#3CB97A' if svc_status.get('analyzor') else '#E04040'}">●</span></div>
      <div class="card-sub">:8000 · FastAPI<br>
        Docling (PDF · Sheets → ledger)<br>
        journal_tech (log + sync GDoc)<br>
        /api/context/structory (contexte LLM)<br>
        /archi (ce diagramme)</div>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--dim)">llmprecogn</div>
      <div class="card-sub">Service VPS indépendant<br>
        Inférence LLM PreCogn<br>
        (séparé d'analyzor)</div>
      <span class="tag x">service propre</span>
    </div>
  </div>
</div>

<div class="arrow">↕</div>

<!-- DONNEES -->
<div class="layer data">
  <div class="lh"><span class="dot"></span>Données — gérées via analyzor / Docling</div>
  <div class="lb">
    <div class="card">
      <div class="card-title" style="color:var(--purple)">Journaux ledger-cli</div>
      <div class="card-sub">~/ledger_api/orgs/&lt;orgId&gt;/journal.ledger<br>
        {ledger_count} orgs actives</div>
      <span class="tag p">Coeur comptable</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--purple)">subscriptions.db</div>
      <div class="card-sub">SQLite · orgs, users, Stripe<br>
        {len(orgs)} orgs enregistrées</div>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--purple)">journal_tech</div>
      <div class="card-sub">~/analyzor/journals/&lt;orgId&gt;/entries.jsonl<br>
        _master = journal global<br>
        Sync Google Doc par org<br>
        Cron 7h17 → snapshot quotidien (Claude)</div>
      <span class="tag p">_master global</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--purple)">Google Drive</div>
      <div class="card-sub">Bibliotheque · scripts GAS<br>
        Drive par org · docs</div>
    </div>
  </div>
</div>

<div class="arrow">↕</div>

<!-- OUTPUT -->
<div class="layer output">
  <div class="lh"><span class="dot"></span>Output</div>
  <div class="lb">
    <div class="card">
      <div class="card-title" style="color:var(--green)">FEC</div>
      <div class="card-sub">Export DGFiP<br>18 champs pipe-séparés</div>
      <span class="tag g">légal FR</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--green)">Rapports Sheets</div>
      <div class="card-sub">Balance · Journal<br>Résultat · Trésorerie<br>Clients · Fournisseurs</div>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--dim)">Liasse fiscale</div>
      <div class="card-sub">À venir</div>
      <span class="tag x">futur</span>
    </div>
    <div class="card">
      <div class="card-title" style="color:var(--dim)">Communication DGFiP</div>
      <div class="card-sub">Envoi automatique<br>impôts / greffes</div>
      <span class="tag x">futur</span>
    </div>
  </div>
</div>

<!-- ORGS -->
<div class="orgs">
  <h2>Organisations de l'écosystème</h2>
  <div class="org-grid">
    {orgs_html}
  </div>
  <div style="color:var(--dim);font-size:9px;margin-top:8px">
    Les organisations clientes (ex: 45 bd Poniatowski → Compta Copro, smcspl → Suivre Mes Comptes) ne sont pas des produits de l'écosystème.
  </div>
</div>

<div class="footer">
  VPS : OVH Ubuntu 22.04 · Disque {disk} ·
  Stripe : TEST uniquement ·
  structory.ai : Cloudflare, géré séparément ·
  Généré à la demande — <a href="/archi" style="color:var(--dim2)">http://213.32.16.118:8000/archi</a>
</div>

</body>
</html>"""
