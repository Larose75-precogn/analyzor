"""
PreCogn Facilitateur — outil Analyzor
Génère / rafraîchit un sheet Google Sheets structuré (Objet/Rule/Flow/Time)
pour une organisation à partir de son journal ledger-cli et de ses briques.

Usage:
    POST /api/precogn/facilitateur
    Body: {orgId, sheetId?: str, rebuild?: bool}

Le sheetId est obligatoire au premier appel (créer le sheet manuellement,
partager le compte de service en éditeur, puis passer l'ID ici).
Les appels suivants utilisent l'ID stocké dans data/{orgId}_facilitateur.json.
"""

import json, re, os, requests as _req
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

_SA_FILE    = Path('/home/ubuntu/analyzor/gdrive-service-account.json')
_DATA_DIR   = Path('/home/ubuntu/analyzor/data')
_LEDGER_URL = 'http://localhost:8080'
_BRICKS_BASE = Path('/home/ubuntu/ledger_api/modules')

# ── Helpers ────────────────────────────────────────────────────────────────────

def _sa_creds(extra_scopes=None):
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    if extra_scopes:
        scopes.extend(extra_scopes)
    return Credentials.from_service_account_file(str(_SA_FILE), scopes=scopes)

def _sheets_svc():
    return build('sheets', 'v4', credentials=_sa_creds())

def _drive_svc():
    return build('drive', 'v3', credentials=_sa_creds(
        extra_scopes=['https://www.googleapis.com/auth/drive.readonly']
    ))

def _read_org_profile(org_id):
    """Lit le profil organisation depuis Drive (brique organisation_*.json)."""
    try:
        registry = json.loads((_DATA_DIR / '_org_registry.json').read_text())
    except Exception:
        return {}
    # Chercher l'entrée dont uid correspond à org_id (ou la clé slug contient 'copro')
    folder_id = None
    for slug, meta in registry.items():
        if meta.get('uid') == org_id or slug == org_id.split('_')[0]:
            folder_id = meta.get('folderId')
            break
    # Fallback : chercher par slug module (ex: compta_copro → uid org_891c...)
    if not folder_id:
        module = _ledger_module(org_id) or ''
        for slug, meta in registry.items():
            if slug == module or slug in org_id:
                folder_id = meta.get('folderId')
                break
    if not folder_id:
        return {}
    try:
        svc = _drive_svc()
        res = svc.files().list(
            q=f"'{folder_id}' in parents and name contains 'organisation_'",
            fields='files(id,name)', pageSize=5
        ).execute()
        files = res.get('files', [])
        if not files:
            return {}
        content = svc.files().get_media(fileId=files[0]['id']).execute()
        return json.loads(content.decode())
    except Exception:
        return {}

def _ledger(org_id, command, filters=None, begin=None, end=None):
    body = {'orgId': org_id, 'command': command, 'filters': filters or []}
    if begin: body['beginDate'] = begin
    if end:   body['endDate']   = end
    r = _req.post(f'{_LEDGER_URL}/api/ledger/query', json=body, timeout=30)
    return r.json().get('output', '')

def _ledger_module(org_id):
    try:
        r = _req.get(f'{_LEDGER_URL}/api/org/{org_id}/module', timeout=10)
        return r.json().get('module')
    except Exception:
        return None

_BAL_RE = re.compile(r'(-?[\d,]+\.\d{2})\s+EUR\s+(.+)')
def _parse_balance(text):
    out = {}
    for line in text.splitlines():
        m = _BAL_RE.search(line)
        if not m: continue
        raw = m.group(2).strip()
        code = raw.split(':')[0].strip()
        try: out[code] = round(float(m.group(1).replace(',', '')), 2)
        except Exception: pass
    return out

_TX_RE = re.compile(r'^(\d{4}/\d{2}/\d{2})\s+[\*!]?\s*(.+)')
def _parse_events(text):
    by_date = defaultdict(list)
    seen = set()
    for line in text.splitlines():
        m = _TX_RE.match(line)
        if not m: continue
        ds, lb = m.group(1), m.group(2).strip().lower()
        key = (ds, lb[:60])
        if key in seen: continue
        seen.add(key)
        by_date[ds].append(lb)
    return by_date

def _read_bricks(module):
    """Retourne toutes les briques JSON du module."""
    path = _BRICKS_BASE / module / 'bricks'
    if not path.exists():
        return []
    bricks = []
    for f in sorted(path.glob('*.json')):
        try:
            bricks.append(json.loads(f.read_text()))
        except Exception:
            pass
    return bricks

def _meta_path(org_id):
    return _DATA_DIR / f'{org_id}_facilitateur.json'

def load_sheet_id(org_id):
    p = _meta_path(org_id)
    if p.exists():
        return json.loads(p.read_text()).get('sheetId')
    return None

def save_sheet_id(org_id, sheet_id):
    _DATA_DIR.mkdir(exist_ok=True)
    _meta_path(org_id).write_text(json.dumps({'orgId': org_id, 'sheetId': sheet_id}))

# ── Objets ─────────────────────────────────────────────────────────────────────

def _build_objets(org_id, module, bricks, org_profile=None):
    """Construit les lignes de l'onglet Objets à partir des balances ledger."""
    current_year = datetime.now().year
    balances = _parse_balance(_ledger(org_id, 'balance', begin=f'{current_year}/01/01'))
    org_profile = org_profile or {}

    rows = [['id', 'object', 'description', 'source']]

    # Organisation elle-même = premier Object PreCogn (spec p.17 : "le premier flux = créé l'org")
    if org_profile:
        contenu = org_profile.get('contenu', {})
        org_desc = (
            f"Organisation PreCogn. Module : {module}. "
            f"Statut : {org_profile.get('status','?')}. "
            f"Langue : {org_profile.get('language','?')}. "
            f"Parent : {contenu.get('parentOrgId','?')}. "
            f"Créée le : {org_profile.get('created','?')[:8]}."
        )
        rows.append([
            org_profile.get('id', org_id),
            org_profile.get('title', org_id),
            org_desc,
            f'Drive:organisation_{org_profile.get("uid","")}.json'
        ])

    # Module compta_copro — enrichissement spécifique
    if module == 'compta_copro':
        rule9 = next((b for b in bricks if 'copropriétaires' in b), None)
        if rule9:
            for c in rule9.get('copropriétaires', []):
                code = c['compte']
                rows.append([code, c['label'],
                    f'Copropriétaire. Quote-part trim : {c["montant_trim"]:.2f} EUR. '
                    f'Solde {current_year} : {balances.get(code, 0)} EUR.',
                    f'Sheet:C - {code}'])

    # Comptes connus du ledger (génériques)
    KNOWN = {
        '5121': 'Banque principale',
        '5011': 'Banque Travaux / Placement',
        '701':  'Provisions charges',
        '105000': 'Fonds travaux ALUR',
        '401':  'Factures fournisseurs',
        '601000': 'Eau de Paris',
        '602000': 'Électricité (EDF)',
        '611002': 'Espace Net (ménage)',
        '614015': 'Maintenance portes',
        '615007': 'Travaux',
        '616002': 'AXA Assurance',
        '622005': 'Frais bancaires',
        '622304': 'Taxe balayage',
        '671134': 'Travaux (acompte)',
    }
    existing_ids = {r[0] for r in rows[1:]}
    for code, nom in KNOWN.items():
        if code in existing_ids: continue
        solde = balances.get(code)
        desc = f'{nom}.'
        if solde is not None:
            desc += f' Solde {current_year} : {solde} EUR.'
        rows.append([code, nom, desc, f'Ledger:{code}'])

    # Briques Rule / Object du module
    for b in bricks:
        bid = b.get('id') or b.get('nom', '')[:20]
        btype = b.get('type', '')
        if btype not in ('Object', 'Règlement', 'Budget'): continue
        if bid in existing_ids: continue
        rows.append([bid, b.get('nom', bid), b.get('description', ''), 'brick:json'])
        existing_ids.add(bid)

    return rows

# ── Rules ──────────────────────────────────────────────────────────────────────

def _brick_title(b):
    """Nom lisible d'une brique : préfère title, sinon nom, sinon id."""
    return b.get('title') or b.get('nom') or b.get('id', '?')

def _brick_desc(b):
    """Description courte d'une brique (première phrase ou 120 chars)."""
    d = b.get('description', '')
    if not d and 'contenu' in b:
        # rule_0007 stocke ses règles dans 'contenu' (dict)
        contenu = b['contenu']
        if isinstance(contenu, dict):
            d = 'Mapping: ' + ', '.join(f"{k}→{v.get('contrepartie','?')}" for k, v in list(contenu.items())[:4]) + '…'
    return d[:200] if d else ''

def _build_rules(org_id, module, bricks):
    rows = [['id', 'title', 'description', 'source']]

    # Lire TOUTES les briques : une brique est une Rule si elle a type==Rule
    # OU si son nom de fichier commence par rule_ (rule_0007 n'a pas de champ type)
    for b in bricks:
        btype = b.get('type', '')
        bid   = b.get('id', '')
        # inclure si type Rule ou si id contient COPRO/COMPTA (briques métier)
        if btype != 'Rule' and not any(k in bid.upper() for k in ('PCG', 'COPRO', 'COMPTA', 'RULE')):
            continue
        title = _brick_title(b)
        desc  = _brick_desc(b)
        src   = f'brick:{bid}'
        rows.append([bid, title, desc, src])

    return rows

# ── Flow ───────────────────────────────────────────────────────────────────────

def _build_flows(org_id, module, bricks):
    rows = [['id', 'titre', 'résumé', 'objets']]
    for i, b in enumerate([b for b in bricks if b.get('type') == 'Flow'], 1):
        pid = b.get('id', f'F{i:03d}')
        rows.append([pid, b.get('titre', b.get('nom', pid)),
                     b.get('résumé', b.get('description', '')),
                     ', '.join(b.get('objets', []))])
    if module == 'compta_copro' and len(rows) == 1:
        rows += [
            ['F001', 'Appel de fonds trimestriel',
             '1) Calculer les 5 quotes-parts. 2) POST /api/copro/appel-fonds. 3) Avis individuel. 4) Email.',
             '451001,451002,451003,451004,451006,701,R001'],
            ['F002', 'Encaissement copropriétaire',
             '1) Virement reçu 5121. 2) Identifier copro. 3) Débit 5121, Crédit 451xxx.',
             '5121,451001,451002,451003,451004,451006'],
            ['F003', 'Paiement fournisseur',
             '1) Facture → Crédit 401 / Débit 6xx. 2) Paiement → Débit 401, Crédit 5121.',
             '401,5121,601000,602000,611002'],
            ['F004', 'Import relevé bancaire',
             '1) Export CSV. 2) POST /api/ledger/convert. 3) Valider mapping. 4) POST /api/ledger/import.',
             '5121,401'],
            ['F005', 'Clôture exercice',
             '1) Balance = 0. 2) journaltosheet → A - Exercice YYYY. 3) Valider C - Balance.',
             '701,5121,5011'],
        ]
    return rows

# ── Time ───────────────────────────────────────────────────────────────────────

def _objets_lies(labels):
    txt = ' '.join(labels)
    refs = []
    if any(k in txt for k in ['appel', 'fonds prevoyance']):      refs += ['R001', 'P001']
    if any(k in txt for k in ['alur', '105000']):                  refs += ['105000', 'R008']
    if any(k in txt for k in ['ag ', 'resolution', 'résolution',
                               'charges réelles', 'solde charges', 'res. ']):
                                                                    refs.append('P005')
    if any(k in txt for k in ['edf', 'total energie', 'electricit']): refs.append('602000')
    if any(k in txt for k in ['eau de paris', 'prlv eau', 'prlvt eau', 'prev eau']):
                                                                    refs.append('601000')
    if any(k in txt for k in ['espace net', 'menage', 'ménage', 'nettoyage', 'defraiment']):
                                                                    refs.append('611002')
    if any(k in txt for k in ['axa', 'assurance', 'desjardins']):  refs.append('616002')
    if any(k in txt for k in ['cogeim', 'syndic', 'patrimonia', 'foncia']):
                                                                    refs.append('622005')
    if 'taxe balayage' in txt:                                      refs.append('622304')
    if any(k in txt for k in ['travaux', 'proreno', 'talibi', 'interphone', 'lierre']):
                                                                    refs.append('5011')
    if any(k in txt for k in ['frais bancaire', 'frais credit', 'sgt frais']): refs.append('622005')
    if any(k in txt for k in ['interets', 'intérêts', 'placement', 'livret']): refs.append('5011')
    COPROS = {'amsellem':'451001','aouchiche':'451002','ben rhouma':'451003',
              'benrhouma':'451003','cholet':'451004','plaissy':'451006'}
    for nom, cpt in COPROS.items():
        if nom in txt: refs.append(cpt)
    seen2, out = set(), []
    for r in refs:
        if r not in seen2: seen2.add(r); out.append(r)
    return ', '.join(out)

def _build_time(org_id, module):
    print_out = _ledger(org_id, 'print')
    by_date = _parse_events(print_out)

    rows = [['id', 'type', 'label', 'date_début', 'date_fin', 'objets_liés']]
    rows.append(['---','---','---','---','---','---'])

    # Années couvertes
    years = sorted({ds[:4] for ds in by_date})
    for i, yr in enumerate(years, 1):
        rows.append([f'T{i:03d}', 'year', yr, f'{yr}/01/01', f'{yr}/12/31', ''])

    # Événements datés
    today = datetime.now().strftime('%Y/%m/%d')
    idx = len(years) + 1
    for ds in sorted(by_date.keys()):
        labels = by_date[ds]
        ol = _objets_lies(labels)
        label = ds.replace('/', '')
        rows.append([f'T{idx:03d}', 'day', label, ds, ds, ol])
        idx += 1

    # Futurs planifiés si module copro
    if module == 'compta_copro':
        if '2026/09/30' not in by_date:
            rows.append([f'T{idx:03d}', 'day', '20260930', '2026/09/30', '2026/09/30',
                         'R001, P001, 451001, 451002, 451003, 451004, 451006'])
            idx += 1
        rows.append([f'T{idx:03d}', 'day', '20261231', '2026/12/31', '2026/12/31', 'P005'])

    return rows

# ── Écriture sheet ─────────────────────────────────────────────────────────────

def _rows_to_values(rows):
    """Convertit une liste de listes en valueRange Sheets API."""
    return rows

def populate_sheet(sheet_id, org_id, module, bricks, org_profile=None):
    svc = _sheets_svc()
    ss = svc.spreadsheets()

    # Récupérer les onglets existants
    meta = ss.get(spreadsheetId=sheet_id, fields='sheets.properties').execute()
    existing = {s['properties']['title']: s['properties']['sheetId']
                for s in meta.get('sheets', [])}

    TAB_BUILDERS = {
        'Objet': lambda: _build_objets(org_id, module, bricks, org_profile),
        'Rule':  lambda: _build_rules(org_id, module, bricks),
        'Flow':  lambda: _build_flows(org_id, module, bricks),
        'Time':  lambda: _build_time(org_id, module),
    }

    # Créer les onglets manquants
    add_reqs = [
        {'addSheet': {'properties': {'title': t}}}
        for t in TAB_BUILDERS if t not in existing
    ]
    if add_reqs:
        res = ss.batchUpdate(spreadsheetId=sheet_id, body={'requests': add_reqs}).execute()
        for rep in res.get('replies', []):
            props = rep.get('addSheet', {}).get('properties', {})
            if props:
                existing[props['title']] = props['sheetId']

    # Supprimer onglets par défaut
    del_reqs = [
        {'deleteSheet': {'sheetId': sid}}
        for title, sid in existing.items()
        if title in ('Sheet1', 'Feuille 1', 'Feuille1') and title not in TAB_BUILDERS
    ]
    if del_reqs:
        ss.batchUpdate(spreadsheetId=sheet_id, body={'requests': del_reqs}).execute()

    # Peupler chaque onglet
    stats = {}
    for tab_name, builder in TAB_BUILDERS.items():
        rows = builder()
        # Clear + update via batchUpdate valueInputOption
        range_name = f"'{tab_name}'!A1"
        ss.values().clear(spreadsheetId=sheet_id, range=range_name).execute()
        ss.values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()
        stats[tab_name] = len(rows) - 2

    return stats

# ── Point d'entrée principal ───────────────────────────────────────────────────

def run(org_id: str, sheet_id: str | None = None, rebuild: bool = False) -> dict:
    """
    Génère ou rafraîchit le sheet facilitateur pour org_id.
    - sheet_id : obligatoire au premier appel (partager le SA en éditeur avant).
    - rebuild  : force le re-peuplement même si le contenu est récent.
    Retourne {sheetId, url, stats}.
    """
    stored = load_sheet_id(org_id)

    if sheet_id and not stored:
        save_sheet_id(org_id, sheet_id)
        stored = sheet_id
    elif not stored:
        raise ValueError(
            'Aucun sheetId connu pour cette org. '
            'Créer un Google Sheet, partager le compte de service en éditeur, '
            'puis passer sheetId dans la requête.'
        )

    target_id = stored
    module = _ledger_module(org_id) or 'compta_copro'
    bricks = _read_bricks(module)
    org_profile = _read_org_profile(org_id)

    stats = populate_sheet(target_id, org_id, module, bricks, org_profile)

    url = f'https://docs.google.com/spreadsheets/d/{target_id}/edit'
    return {'sheetId': target_id, 'url': url, 'module': module, 'stats': stats}
