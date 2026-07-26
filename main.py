from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
import uvicorn
import tempfile
import os
import json
import requests
from typing import Optional

from journal_engine import (
    extract_account_code, extract_account_label, extract_group_prefix,
    looks_like_ledger_table, looks_like_grouped_account_blocks,
    extract_ledger_postings, extract_grouped_expense_postings, reconcile,
    build_ledger_text, build_ledger_entries,
)

LEDGER_API_URL = os.environ.get('LEDGER_API_URL', 'http://localhost:8080')
from config_resolver import resolve_query_keywords, resolve_table_config, resolve_connectors

app = FastAPI(
    title="Analyzor",
    description="PreCogn Document Analysis Engine",
    version="0.3.0"
)

# Docling — accès exclusivement via le connector (connector_docling.py).
# Ce fichier ne doit plus jamais importer `docling.*` directement.
try:
    from connector_docling import extract_sheets
    DOCLING_AVAILABLE = True
    print("✅ Docling disponible (via connector)")
except ImportError:
    DOCLING_AVAILABLE = False
    extract_sheets = None
    print("⚠️ Docling non installé")

import connector_ollama

@app.get("/")
async def root():
    return {
        "name": "Analyzor",
        "version": "0.3.0",
        "status": "running",
        "docling": DOCLING_AVAILABLE,
        "routes": ["/", "/health", "/upload", "/sheettojournal", "/api/context/query-keywords"]
    }

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.get("/api/ollama/status")
async def ollama_status():
    return connector_ollama.status()

@app.get("/api/ollama/models")
async def ollama_models():
    return connector_ollama.models()

@app.post("/api/ollama/generate")
async def ollama_generate(payload: dict):
    return connector_ollama.generate(payload)

@app.post("/api/ollama/chat")
async def ollama_chat(payload: dict):
    return connector_ollama.chat(payload)

@app.post("/api/ollama/embed")
async def ollama_embed(payload: dict):
    return connector_ollama.embed(payload)

@app.get("/api/context/query-keywords")
async def context_query_keywords():
    """Vocabulaire de reconnaissance des consultations (garde-fou déterministe
    côté Communicator) — donnée (briques Rule), pas code, pour pouvoir être
    complétée sans redéploiement de Communicator."""
    return {"success": True, "keywords": resolve_query_keywords()}

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    try:
        # 1. Lire le fichier
        content = await file.read()
        
        if not content:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": "Fichier vide"}
            )
        
        # 2. Sauvegarder temporairement
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        
        # 3. Si Docling est disponible, analyser
        markdown = None
        if DOCLING_AVAILABLE and converter:
            try:
                result = converter.convert(tmp_path)
                markdown = result.document.export_to_markdown()
            except Exception as e:
                markdown = f"Erreur Docling: {str(e)}"
        else:
            markdown = f"# Document: {file.filename}\n\nTaille: {len(content)} octets\nDocling non disponible."
        
        # 4. Nettoyer
        os.unlink(tmp_path)
        
        return {
            "status": "ok",
            "name": file.filename,
            "size": len(content),
            "docling_available": DOCLING_AVAILABLE,
            "markdown": markdown
        }
        
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except:
            pass
        
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": str(e),
                "type": str(type(e).__name__)
            }
        )

@app.post("/sheettojournal")
async def sheettojournal(file: UploadFile = File(...), orgId: Optional[str] = Form(None), module: Optional[str] = Form(None)):
    """Point d'entrée analyzor pour sheettojournal.

    Orchestration explicite ici (pas dans le connector, pas caché ailleurs) :
    - PPDC : un onglet à la fois (1 onglet = 1 unité de travail).
    - Entonnoir : à chaque onglet, 3 niveaux qui éliminent progressivement
      avant d'extraire quoi que ce soit.

    Si `orgId` est fourni, les écritures réconciliées sont envoyées à
    coeur_comptable (ledger_api /api/ledger/import) - Analyzor ne fait ici que
    l'extraction/réconciliation (aucune logique métier propre au Connector),
    l'écriture réelle dans le journal appartient à coeur_comptable.
    """
    if not DOCLING_AVAILABLE:
        return JSONResponse(status_code=503, content={"status": "error", "error": "Docling non installé"})

    content = await file.read()
    if not content:
        return JSONResponse(status_code=400, content={"status": "error", "error": "Fichier vide"})

    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    # Config résolue en cascade (org -> module -> Structory) une seule fois
    # pour tout le classeur - jamais de constante codée en dur ici.
    table_config = resolve_table_config(org_id=orgId, module=module)
    ledger_patterns = table_config.get('ledger_header_patterns')
    min_header_matches = table_config.get('min_header_matches', 3)
    skip_labels = table_config.get('skip_labels')
    block_total_tolerance = table_config.get('block_total_tolerance', 0.02)
    partial_block_policy = table_config.get('partial_block_policy', 'keep_with_flag')

    try:
        sheets = extract_sheets(tmp_path)  # <- seul appel au connector

        onglets_rapport = []
        postings_par_onglet = {}  # nom d'onglet -> postings candidats (avant sélection de groupe)

        # PPDC : boucle explicite, un onglet à la fois
        for sheet in sheets:
            groupe = extract_group_prefix(sheet.name)

            # Niveau 1 (le plus grossier) : y a-t-il au moins une table ?
            if not sheet.tables:
                onglets_rapport.append({
                    "onglet": sheet.name, "groupe": groupe, "niveau_atteint": 1,
                    "classification": "vide_ou_non_tabulaire"
                })
                continue

            table = sheet.tables[0]  # table principale (grand livre) si l'onglet en est un

            # Niveau 2 : la table ressemble-t-elle à un grand livre ?
            # Générique : certaines tables ont une ligne "titre" (ex: code+nom de
            # compte) avant les vrais en-têtes de colonnes. On vérifie donc les
            # en-têtes bruts, et si ça ne matche pas, la 1re ligne de données.
            en_tete_reel = table.headers
            lignes_donnees = table.rows
            if (not looks_like_ledger_table(table.headers, ledger_patterns, min_header_matches)
                    and table.rows
                    and looks_like_ledger_table(table.rows[0], ledger_patterns, min_header_matches)):
                en_tete_reel = table.rows[0]
                lignes_donnees = table.rows[1:]

            est_grand_livre = looks_like_ledger_table(en_tete_reel, ledger_patterns, min_header_matches)

            if not est_grand_livre:
                # Forme alternative : collection de blocs de compte groupés
                # (ex. un onglet "dépenses" avec 1 table par compte x période,
                # en-tête [code, libellé, total] suivi de lignes [date, montant]).
                if looks_like_grouped_account_blocks(sheet.tables):
                    bloc_postings, incertains = extract_grouped_expense_postings(
                        sheet.tables, block_total_tolerance=block_total_tolerance, partial_block_policy=partial_block_policy
                    )
                    postings_par_onglet[sheet.name] = bloc_postings
                    onglets_rapport.append({
                        "onglet": sheet.name, "groupe": groupe, "niveau_atteint": 4,
                        "classification": "blocs_de_compte_groupes",
                        "n_tables": len(sheet.tables),
                        "n_postings_extraits": len(bloc_postings),
                        "n_blocs_incertains": len(incertains),
                        "besoin_llm": len(incertains) > 0,
                    })
                    continue

                onglets_rapport.append({
                    "onglet": sheet.name, "groupe": groupe, "niveau_atteint": 2,
                    "classification": "non_grand_livre",
                    "n_lignes": table.n_rows, "n_colonnes": table.n_cols,
                    "besoin_llm": True  # à classer parmi Object/vue dérivée/référence/indéterminé
                })
                continue

            # Niveau 3 : le nom de l'onglet porte-t-il un code de compte ?
            compte = extract_account_code(sheet.name)
            if compte is None:
                onglets_rapport.append({
                    "onglet": sheet.name, "groupe": groupe, "niveau_atteint": 3,
                    "classification": "grand_livre_sans_compte_identifie",
                    "n_lignes": table.n_rows, "n_colonnes": table.n_cols,
                    "besoin_llm": True  # nom d'onglet à interpréter
                })
                continue

            # Niveau 4 (le plus fin) : tout est validé, extraction retenue
            label = extract_account_label(sheet.name)
            sheet_postings = extract_ledger_postings(compte, en_tete_reel, lignes_donnees, label=label, skip_labels=skip_labels)
            postings_par_onglet[sheet.name] = sheet_postings
            onglets_rapport.append({
                "onglet": sheet.name, "groupe": groupe, "niveau_atteint": 4,
                "classification": "grand_livre_identifie",
                "compte": compte,
                "n_lignes": table.n_rows, "n_colonnes": table.n_cols,
                "n_postings_extraits": len(sheet_postings),
                "besoin_llm": False
            })

        # Sélection de groupe : quand le classeur range ses onglets en groupes
        # numérotés ('1 - ...', '2 - ...'), seul le groupe qui contient les
        # onglets nommés directement par un code de compte (grand_livre_identifie)
        # est une source de vérité pour l'extraction - les autres groupes sont
        # des vues/rapports dérivés du même groupe et double-compteraient les
        # mêmes écritures s'ils étaient extraits aussi. Explicite dans le
        # rapport, pas une exclusion silencieuse.
        groupes_source = {
            o["groupe"] for o in onglets_rapport
            if o["classification"] == "grand_livre_identifie" and o["groupe"] is not None
        }
        groupe_retenu = sorted(groupes_source, key=int)[0] if groupes_source else None

        postings = []
        for o in onglets_rapport:
            if o["onglet"] not in postings_par_onglet:
                continue
            if groupe_retenu is not None and o["groupe"] != groupe_retenu:
                o["classification"] += "_hors_groupe_source"
                o["exclu_de_extraction"] = True
                o["besoin_llm"] = False
                continue
            postings.extend(postings_par_onglet[o["onglet"]])

        # Réconciliation en partie double, sur l'ensemble des postings du groupe source
        transactions, unmatched_debits, unmatched_credits = reconcile(postings)
        n_matched = len(transactions)
        n_unmatched = len(unmatched_debits) + len(unmatched_credits)
        n_ecritures = n_matched + n_unmatched
        taux_reconciliation = round(n_matched / n_ecritures, 4) if n_ecritures else None

        ledger_text = build_ledger_text(
            transactions, unmatched_debits, unmatched_credits,
            header_comment=f"Généré par /sheettojournal depuis {file.filename}",
        )

        n_retenus = sum(
            1 for o in onglets_rapport
            if o["classification"] in ("grand_livre_identifie", "blocs_de_compte_groupes")
        )

        import_coeur_comptable = None
        if orgId:
            entries = build_ledger_entries(transactions, unmatched_debits, unmatched_credits)
            try:
                resp = requests.post(
                    f"{LEDGER_API_URL}/api/ledger/import",
                    json={"orgId": orgId, "source": f"sheettojournal:{file.filename}", "entries": entries, "mode": "replace"},
                    timeout=30,
                )
                import_coeur_comptable = resp.json()
                import_coeur_comptable["http_status"] = resp.status_code
            except requests.RequestException as e:
                import_coeur_comptable = {"success": False, "error": f"coeur_comptable injoignable: {e}"}

        return {
            "status": "ok",
            "name": file.filename,
            "n_onglets": len(sheets),
            "n_onglets_retenus": n_retenus,
            "onglets": onglets_rapport,
            "reconciliation": {
                "n_postings_extraits": len(postings),
                "n_ecritures": n_ecritures,
                "n_appariees": n_matched,
                "n_non_appariees": n_unmatched,
                "taux_reconciliation": taux_reconciliation,
            },
            "ledger": ledger_text,
            "import_coeur_comptable": import_coeur_comptable,
        }

    finally:
        os.unlink(tmp_path)


# ============================================================
# JOURNAL TECHNIQUE PAR ORGANISATION (demandé par Stéphane 2026-07-18)
# ============================================================
from fastapi.responses import HTMLResponse
import journal as _journal

SUBSCRIPTIONS_URL = os.environ.get('SUBSCRIPTIONS_URL', 'http://localhost:8082')
SUBSCRIPTIONS_SERVICE_KEY = '***REMOVED_SERVICE_KEY***'


def _can_read_journal(requester_org_id, target_org_id):
    """Contrôle d'accès (storage/PreCogn/rule.0005.journal-technique-universel.json, id
    R-STRUCTORY-JOURNAL-0001) : même org, org parente d'une
    org fille, ou demande d'accès accordée — voir subscriptions_api/db.py::can_read_journal.
    Fail-closed : si subscriptions_api est injoignable, on refuse plutôt que d'exposer un
    journal technique par erreur (contrairement au fail-open des autres services de cet
    écosystème, qui protègent l'usage courant plutôt que la confidentialité)."""
    try:
        r = requests.get(
            f'{SUBSCRIPTIONS_URL}/api/access/can-read',
            params={'requesterOrgId': requester_org_id, 'targetOrgId': target_org_id},
            headers={'X-Service-Key': SUBSCRIPTIONS_SERVICE_KEY},
            timeout=3,
        )
        r.raise_for_status()
        return r.json().get('canRead', False)
    except requests.RequestException:
        return False


@app.post("/api/journal/log")
async def journal_log(payload: dict):
    """Body: {orgId, actor, summary, details?: [str]}. N'importe quel service de
    l'écosystème peut logger une action ici. Synchronise aussi vers le Google Doc
    dédié à l'organisation (créé au premier appel, mis à jour ensuite)."""
    org_id = payload.get("orgId")
    actor = payload.get("actor")
    summary = payload.get("summary")
    if not org_id or not actor or not summary:
        return JSONResponse({"success": False, "error": "orgId, actor et summary requis"}, status_code=400)

    entry = _journal.log_action(org_id, actor, summary, payload.get("details"))
    try:
        gdoc_id, created = _journal.sync_to_gdoc(org_id)
        gdoc_url = f"https://docs.google.com/document/d/{gdoc_id}/edit"
        gdoc_error = None
    except Exception as e:
        gdoc_id, created, gdoc_url = None, False, None
        gdoc_error = str(e)

    return {"success": True, "entry": entry, "gdocUrl": gdoc_url, "gdocError": gdoc_error}


@app.get("/api/journal/html")
async def journal_html(orgId: str, requesterOrgId: str):
    """requesterOrgId : l'organisation qui consulte (elle-même, une org parente, ou une org
    ayant reçu un accès accordé — voir /api/access/* dans subscriptions_api)."""
    if not _can_read_journal(requesterOrgId, orgId):
        return JSONResponse(
            {"success": False, "error": f"{requesterOrgId} n'a pas accès au journal de {orgId} "
                                         f"(demander via POST /api/access/request sur subscriptions_api)"},
            status_code=403,
        )
    return HTMLResponse(_journal.render_html(orgId))


@app.post("/api/journal/register-gdoc")
async def journal_register_gdoc(payload: dict):
    """Enregistre l'id d'un Google Doc déjà créé (par un vrai compte humain, ex. via
    l'outil Drive de Claude) comme journal de l'org — nécessaire car le compte de
    service n'a aucun quota Drive propre et ne peut PAS créer de nouveau fichier
    (`storageQuotaExceeded`), seulement mettre à jour un fichier déjà possédé par un
    humain et partagé avec lui. Bootstrap à faire une fois par nouvelle organisation."""
    org_id = payload.get("orgId")
    file_id = payload.get("fileId")
    if not org_id or not file_id:
        return JSONResponse({"success": False, "error": "orgId et fileId requis"}, status_code=400)
    _journal._org_dir(org_id)
    with open(_journal._gdoc_id_path(org_id), "w") as f:
        f.write(file_id)
    return {"success": True}


@app.get("/api/journal/gdoc")
async def journal_gdoc(orgId: str, requesterOrgId: str):
    """Renvoie l'URL du Google Doc de l'org (le crée s'il n'existe pas encore). Même contrôle
    d'accès que /api/journal/html."""
    if not _can_read_journal(requesterOrgId, orgId):
        return JSONResponse(
            {"success": False, "error": f"{requesterOrgId} n'a pas accès au journal de {orgId}"},
            status_code=403,
        )
    try:
        gdoc_id, created = _journal.sync_to_gdoc(orgId)
        return {"success": True, "gdocUrl": f"https://docs.google.com/document/d/{gdoc_id}/edit", "created": created}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── journaltosheet ────────────────────────────────────────────────────────────
import re as _re

@app.post("/api/journaltosheet")
async def journaltosheet(payload: dict):
    """Régénère le sheet miroir depuis le journal ledger-cli.

    Pour chaque onglet section 1 du sheet (compte comptable), lit le registre
    ledger correspondant et réécrit les lignes de données dans l'onglet — sans
    toucher aux lignes 1 (en-tête compte) et 2 (colonnes). C'est le Navigator :
    le sheet reflète toujours l'état du journal, pas l'inverse.

    Body: {orgId, sheetId, ledgerApiUrl?: str}
    """
    org_id = payload.get('orgId')
    sheet_id = payload.get('sheetId')
    ledger_url = payload.get('ledgerApiUrl') or LEDGER_API_URL

    if not org_id or not sheet_id:
        return JSONResponse({'success': False, 'error': 'orgId et sheetId requis'}, status_code=400)

    from connector_ownstorage import get_sheet_tabs, write_sheet_range, clear_sheet_range

    try:
        tabs = get_sheet_tabs(sheet_id)
    except Exception as e:
        return JSONResponse({'success': False, 'error': f'Erreur lecture tabs: {e}'}, status_code=500)

    rapport = []

    for tab in tabs:
        title = tab['title']
        if not title.startswith('1 - '):
            continue
        if title in ('1 - Balance', '1 - SYNTHESE'):
            continue
        if '6 DEPENSES' in title:
            continue

        # Extraire le code compte depuis le nom de l'onglet
        m = _re.search(r'\b(\d{3,6})\b', title)
        if not m:
            continue
        compte = m.group(1)

        # Lire le registre ledger pour ce compte
        try:
            resp = requests.post(
                f'{ledger_url}/api/ledger/query',
                json={'orgId': org_id, 'command': 'register', 'filters': [compte]},
                timeout=15,
            )
            result = resp.json()
        except Exception as e:
            rapport.append({'onglet': title, 'status': 'erreur', 'error': str(e)})
            continue

        if not result.get('success'):
            rapport.append({'onglet': title, 'status': 'erreur', 'error': result.get('error')})
            continue

        # Parser la sortie ledger register
        # Format réel : "22-Jan-01 Description    COMPTE    699.00 EUR   699.00 EUR"
        # Date au format YY-Mon-DD, solde parfois juste "0" sans EUR
        from datetime import datetime as _dt
        rows = []
        for line in (result.get('output') or '').splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            parts = _re.split(r'\s{2,}', line.strip())
            if len(parts) < 3:
                continue
            date_desc = parts[0]
            date_m = _re.match(r'^(\d{2}-[A-Za-z]{3}-\d{2})\s+(.*)', date_desc)
            if not date_m:
                continue
            try:
                date_obj = _dt.strptime(date_m.group(1), '%y-%b-%d')
                date_str = date_obj.strftime('%Y/%m/%d')
            except ValueError:
                continue
            # strip account name from description if not merged (4+ parts)
            libelle_raw = date_m.group(2)
            if len(parts) >= 4:
                libelle_raw = libelle_raw  # account is in parts[1], desc clean
            libelle = libelle_raw.rstrip('. ')
            montant_raw = parts[-2].replace(' EUR', '').replace(',', '').strip()
            solde_raw   = parts[-1].replace(' EUR', '').replace(',', '').strip()
            try:
                montant = float(montant_raw)
                solde   = float(solde_raw) if solde_raw not in ('0', '') else 0.0
            except ValueError:
                continue
            debit  = round(montant, 2) if montant > 0 else ''
            credit = round(-montant, 2) if montant < 0 else ''
            rows.append([date_str, libelle, debit, credit, round(solde, 2)])

        if not rows:
            rapport.append({'onglet': title, 'status': 'vide', 'n': 0})
            continue

        # Écrire dans l'onglet : ligne 3 onwards (lignes 1+2 = en-têtes intouchées)
        range_name = f"'{title}'!A3:E{2 + len(rows)}"
        try:
            clear_sheet_range(sheet_id, f"'{title}'!A3:E1000")
            write_sheet_range(sheet_id, range_name, rows)
            rapport.append({'onglet': title, 'status': 'ok', 'compte': compte, 'n': len(rows)})
        except Exception as e:
            rapport.append({'onglet': title, 'status': 'erreur_ecriture', 'error': str(e)})

    # ── Onglet Journal : toutes les écritures (format csv = noms complets) ──────
    import csv as _csv, io as _io
    try:
        resp_j = requests.post(
            f'{ledger_url}/api/ledger/query',
            json={'orgId': org_id, 'command': 'csv'},
            timeout=30,
        )
        csv_all = resp_j.json()
    except Exception as e:
        csv_all = {'success': False, 'error': str(e)}

    if csv_all.get('success'):
        # CSV colonnes : date, ?, libellé, compte, devise, montant, *, ?
        # Noms → codes anonymes (noms de famille + prénoms connus)
        _noms_anon = [
            ('AMSELLEM',    '451001 COPRO-A'),
            ('AOUCHICHE',   '451002 COPRO-B'),
            ('BENRHOUMA',   '451003 COPRO-C'),
            ('BEN RHOUMA',  '451003 COPRO-C'),
            ('CHOLET',      '451004 COPRO-D'),
            ('chollet',     '451004 COPRO-D'),   # typo source fréquent
            ('PLAISSY',     '451006 COPRO-E'),
            # Prénoms
            ('hamadi',      'COPRO-C'),
            ('saliha',      'COPRO-B'),
            ('rachel',      'COPRO-A'),
            ('grimmer',     'COPRO-A'),
            ('michèle',     'COPRO-D'),
            ('michele',     'COPRO-D'),
            ('stéphane',    'COPRO-E'),
            ('stephane',    'COPRO-E'),
            ('emmanuelle',  'COPRO-E'),
        ]

        def _anon_lib(libelle):
            lib = libelle
            for old, new in _noms_anon:
                lib = _re.sub(_re.escape(old), new, lib, flags=_re.IGNORECASE)
            return lib

        # Mots-clés (en majuscules, comparés à libelle_up) → compte de contrepartie
        _libelle_compte_rules = [
            (['TRAVAUX PLACEMENT', 'COMPTE TRAVAUX', 'SOLDE BRED', 'BRED TRAVAUX',
              'SOLDE TRESORERIE', 'VIREMENT SOLDE'], '5011'),
            (['TOTAL ENERGIE', 'EDF', 'EAU DE PARIS', 'EAU PARIS', 'ESPACE NET',
              'COGEIM', 'FONCIA', 'PRORENO', 'AXA', 'REMBT FACTURE', 'FRAIS BANQUE',
              'SGT', 'CHQ ', 'PRLVT', 'PRLT'], '401'),
        ]

        def _resolve_compte(compte, libelle_up):
            if 'Attente' not in compte:
                return compte.split(':')[0] if ':' in compte else compte
            # Résolution par mots-clés du libellé (avant la recherche copropriétaire)
            for keywords, target in _libelle_compte_rules:
                if any(kw in libelle_up for kw in keywords):
                    return target
            _copro = None
            for kw, code in [
                ('451001', '451001'), ('COPRO-A', '451001'),
                ('451002', '451002'), ('COPRO-B', '451002'),
                ('451003', '451003'), ('COPRO-C', '451003'),
                ('451004', '451004'), ('COPRO-D', '451004'),
                ('451006', '451006'), ('COPRO-E', '451006'),
            ]:
                if kw in libelle_up:
                    _copro = code
                    break
            if 'entree-banque' in compte:
                return _copro or '451xxx'
            if 'sortie-banque' in compte:
                return _copro or '401'
            if 'sortie-tvx' in compte:
                return '5011→5121'
            if '45x' in compte:
                return _copro or '451xxx (appel)'
            if '6xx' in compte:
                return '6xx Charges'
            if 'fonds' in compte:
                return '1xx Fonds'
            return compte.split(':')[-1]

        # ── Grouper par transaction (date + libellé original) ──────────────
        # Chaque transaction = liste de postings ; on déduplique ensuite par
        # signature (date + frozenset des (compte_clean, montant)).
        from collections import OrderedDict as _OD
        txn_map = _OD()   # (date, orig_libelle) → {lib_display, postings: [(compte, amount)]}
        for row in _csv.reader(_io.StringIO(csv_all.get('output', ''))):
            if len(row) < 6:
                continue
            date_str, _, libelle, compte, devise, amount_raw = row[0], row[1], row[2], row[3], row[4], row[5]
            if compte in ('998', '999'):
                continue
            try:
                amount = round(float(amount_raw), 2)
            except ValueError:
                continue
            key = (date_str, libelle)
            if key not in txn_map:
                txn_map[key] = {'date': date_str, 'lib': _anon_lib(libelle), 'postings': []}
            txn_map[key]['postings'].append((compte, amount))

        # ── Identifier les libellés d'appels trimestriels détaillés par copro ──
        # Si le journal a déjà les appels individuels par copropriétaire (451xxx / 701),
        # on masque les entrées agrégées "Attente:45x / 701" du même trimestre.
        _appel_quarters_with_detail = set()
        for txn in txn_map.values():
            postings_comptes = [c for c,_ in txn['postings']]
            has_451 = any(c.startswith('451') and len(c)==6 for c in postings_comptes)
            has_701 = any('701' in c for c in postings_comptes)
            if has_451 and has_701:
                # Trimestre détecté depuis le libellé (ex: "Appel 3eme trim 2026" → "3T26")
                m = _re.search(r'(\d)(?:er|eme|T)\s*(?:trim(?:estre)?\s*)?(\d{2,4})', txn['lib'], _re.I)
                if m:
                    _appel_quarters_with_detail.add((m.group(1), m.group(2)[-2:]))

        def _is_aggregate_appel(lib, postings):
            """Vrai si cette transaction est l'appel agrégé (Attente:45x/701)
            alors que les détails par copropriétaire existent déjà."""
            has_attente45x = any('45x' in c or 'Attente' in c for c, _ in postings)
            has_701 = any('701' in c for c, _ in postings)
            if not (has_attente45x and has_701):
                return False
            m = _re.search(r'(\d)(?:er|eme|T)\s*(?:trim(?:estre)?\s*)?(\d{2,4})', lib, _re.I)
            if m and (m.group(1), m.group(2)[-2:]) in _appel_quarters_with_detail:
                return True
            return False

        # ── Dédupliquer par signature ──────────────────────────────────────
        seen_sigs = set()
        data_rows = []
        for (date_str, orig_lib), txn in txn_map.items():
            lib = txn['lib']
            lib_up = lib.upper()
            # Masquer les appels agrégés redondants
            if _is_aggregate_appel(lib_up, txn['postings']):
                continue
            resolved = [(date_str, _resolve_compte(c, lib_up), a) for c, a in txn['postings']]
            sig = (date_str, frozenset((c, a) for _, c, a in resolved))
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            for d, compte_clean, amount in resolved:
                debit  = amount if amount > 0 else ''
                credit = round(-amount, 2) if amount < 0 else ''
                data_rows.append((date_str, [date_str, lib, compte_clean, debit, credit]))

        # Trier par date décroissante (plus récente en premier)
        data_rows.sort(key=lambda x: x[0], reverse=True)
        journal_rows = [['Date', 'Libellé', 'Compte', 'Débit', 'Crédit']] + [r for _, r in data_rows]

        # Trouver ou créer l'onglet Journal
        journal_tab = next((t for t in tabs if t['title'] == 'Journal'), None)
        if journal_tab:
            try:
                clear_sheet_range(sheet_id, "Journal!A1:E10000")
                write_sheet_range(sheet_id, f"Journal!A1:E{len(journal_rows)}", journal_rows)
                rapport.append({'onglet': 'Journal', 'status': 'ok', 'n': len(journal_rows) - 1})
            except Exception as e:
                rapport.append({'onglet': 'Journal', 'status': 'erreur_ecriture', 'error': str(e)})
        else:
            rapport.append({'onglet': 'Journal', 'status': 'onglet_absent',
                            'note': 'Créer un onglet nommé "Journal" dans le sheet pour l\'activer'})

    n_ok = sum(1 for r in rapport if r['status'] == 'ok')
    return {'success': True, 'tabs_traites': len(rapport), 'tabs_ok': n_ok, 'rapport': rapport}


# IDENTITÉ EN BRIQUES (Organisation / User) — Stéphane, 2026-07-20 : l'organisation est
# l'unité de base, une brique User appartient à son organisation (pas d'identité globale),
# Analyzor porte la reconnaissance ("il le connaît déjà ?") via bricks.py. Remplace le SQLite
# de subscriptions_api comme source de vérité pour l'identité/organisation.
# ============================================================

import bricks as _bricks


@app.post("/api/org/create")
async def org_create(payload: dict):
    """Body: {name}. org_id = nom slugifié par l'organisation elle-même ; si déjà pris,
    errorCode org_id_taken pour que l'appelant en propose un autre (jamais de suffixe auto)."""
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"success": False, "errorCode": "name_required"}, status_code=400)
    result = _bricks.create_org(name)
    status = 200 if result.get("success") else (409 if result.get("errorCode") == "org_id_taken" else 400)
    return JSONResponse(result, status_code=status)


@app.get("/api/org/{org_id}")
async def org_get(org_id: str):
    org = _bricks.get_org(org_id)
    if not org:
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    # folderId : nécessaire côté Apps Script (ConnectorIdentity.js) pour agir en écriture sur
    # ce dossier avec l'identité du visiteur (DriveApp.getFolderById) — Analyzor ne peut que le
    # retrouver/lire, jamais y écrire lui-même (pas de quota d'écriture propre, voir
    # connector_ownstorage.py).
    return {"success": True, "org": org, "members": _bricks.list_users(org_id), "folderId": _bricks._folder_id_for_org(org_id)}


@app.post("/api/org/{org_id}/address")
async def org_register_address(org_id: str, payload: dict):
    """Enregistre l'adresse BYOS (dossier Drive aujourd'hui) d'une organisation créée en
    libre-service dans Docling — le registre (et bus d'adressage) de l'écosystème PreCogn,
    voir docling_registry.py. Appelé par ConnectorDocling.js (bibliotheque) juste après la
    création, remplace la recherche plein-texte Drive-wide comme mécanisme principal de
    résolution org_id → dossier.

    Body: {uid, folderId, backend?}"""
    folder_id = (payload.get("folderId") or "").strip()
    uid = (payload.get("uid") or "").strip()
    if not folder_id or not uid:
        return JSONResponse({"success": False, "errorCode": "uid_et_folderId_requis"}, status_code=400)
    _bricks.register_org_address(org_id, uid, folder_id, backend=payload.get("backend") or "gdrive")
    return {"success": True}


@app.post("/api/org/{org_id}/user")
async def org_add_user(org_id: str, payload: dict):
    """Body: {email, name?, role?}. Écrit une brique User dans le dossier de cette org.
    Idempotent par email dans cette org précise (voir bricks.create_user_in_org)."""
    email = (payload.get("email") or "").strip()
    result = _bricks.create_user_in_org(org_id, email, name=payload.get("name"), role=payload.get("role") or "editor")
    status = 200 if result.get("success") else (404 if result.get("errorCode") == "unknown_org" else 400)
    return JSONResponse(result, status_code=status)


@app.get("/api/account/lookup-by-email")
async def account_lookup_by_email(email: str):
    """Toutes les briques User connues pour cet email, tous dossiers d'org confondus —
    répond à "Analyzor connaît-il déjà ce user ?" (index en mémoire, bricks.py)."""
    return {"success": True, "memberships": _bricks.lookup_by_email(email)}


@app.post("/api/org/{org_id}/secrets")
async def org_set_secret(org_id: str, payload: dict):
    """Stocke un secret (ex. clé API d'un connector) chiffré dans le Drive de cette org
    uniquement (org_secrets.py) — jamais retourné en clair par un endpoint GET, voir
    /api/org/{org_id}/secrets pour la liste des noms seulement.

    Body: {name, value}"""
    import org_secrets
    name = (payload.get('name') or '').strip()
    value = payload.get('value')
    if not name or not value:
        return JSONResponse({"success": False, "errorCode": "name_ou_value_manquant"}, status_code=400)
    result = org_secrets.set_secret(org_id, name, value)
    status = 200 if result.get('success') else (404 if result.get('errorCode') == 'unknown_org' else 400)
    return JSONResponse(result, status_code=status)


@app.get("/api/org/{org_id}/secrets")
async def org_list_secrets(org_id: str):
    """Liste les NOMS des secrets configurés pour cette org — jamais les valeurs."""
    import org_secrets
    if not _bricks._folder_id_for_org(org_id):
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    return {"success": True, "secretNames": org_secrets.list_secret_names(org_id)}


@app.get("/api/org/{org_id}/secrets/{name}/value")
async def org_get_secret_value(org_id: str, name: str, request: Request):
    """Route STRICTEMENT interne (jamais documentée publiquement, jamais appelée par un outil
    consommateur type Communicator) : seul un backend de confiance qui a besoin d'utiliser
    réellement le secret (ex. l'Executor pour envoyer un email via le SMTP configuré par
    l'org) peut l'appeler, avec la même clé partagée que subscriptions_api. Ne renvoie jamais
    ce secret à un client final — voir GET /api/org/{org_id}/secrets pour l'écran d'admin
    (noms seulement)."""
    import org_secrets
    if request.headers.get('X-Service-Key') != SUBSCRIPTIONS_SERVICE_KEY:
        return JSONResponse({"success": False, "errorCode": "unauthorized"}, status_code=401)
    value = org_secrets.get_secret(org_id, name)
    if value is None:
        return JSONResponse({"success": False, "errorCode": "secret_introuvable"}, status_code=404)
    return {"success": True, "value": value}


@app.post("/api/org/{org_id}/comptes")
async def org_add_compte(org_id: str, payload: dict):
    """Ajoute un compte patrimoine à une organisation (Suivre Mes Comptes ARCHITECTURE.md
    §1.1) — à la main (formulaire/Navigator) ou par API, jamais déduit automatiquement.

    Body: {etablissement, titulaire, nom, nature: "courant"|"épargne"|"titres"|"assurance_vie",
           devise_origine}"""
    result = _bricks.create_compte(org_id, payload)
    if result.get('success'):
        return result
    status = 404 if result.get('errorCode') == 'unknown_org' else 400
    return JSONResponse(result, status_code=status)


@app.get("/api/org/{org_id}/comptes")
async def org_list_comptes(org_id: str):
    """Liste les comptes patrimoine d'une organisation — utilisé par le Navigator et
    l'Executor (via /api/org/{org_id}/bricks?type=Compte, équivalent générique)."""
    if not _bricks._folder_id_for_org(org_id):
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    return {"success": True, "comptes": _bricks.list_comptes(org_id)}


@app.get("/api/org/{org_id}/bricks")
async def org_bricks(org_id: str, type: Optional[str] = None):
    """Liste les briques d'un type donné dans le dossier Drive de cette org (générique —
    utilisé notamment par l'Executor pour lister les objets Compte d'une organisation,
    Suivre Mes Comptes ARCHITECTURE.md §1.1)."""
    folder_id = _bricks._folder_id_for_org(org_id)
    if not folder_id:
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    return {"success": True, "bricks": _bricks.list_bricks(folder_id, type)}


@app.get("/api/connectors/resolve")
async def connectors_resolve(etablissement: str, nature: str, orgId: Optional[str] = None, module: Optional[str] = None):
    """Résout les connectors compatibles pour un établissement + une nature de compte
    (Suivre Mes Comptes ARCHITECTURE.md §6) — utilisé par l'Executor, jamais par
    l'utilisateur ni le Navigator directement (§2)."""
    matches = resolve_connectors(etablissement, nature, org_id=orgId, module=module)
    return {"success": True, "connectors": matches}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
