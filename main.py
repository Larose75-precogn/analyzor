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
from config_resolver import (
    resolve_query_keywords, resolve_table_config, resolve_connectors,
    invalidate_module_cache, MODULE_FOLDER_ID,
)

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
import connector_llmprecogn
import understand as _understand
import docling_registry

@app.get("/")
async def root():
    return {
        "name": "Analyzor",
        "version": "0.3.0",
        "status": "running",
        "docling": DOCLING_AVAILABLE,
        "routes": ["/", "/health", "/upload", "/sheettojournal",
                   "/api/context/query-keywords",
                   "/api/ollama/status", "/api/ollama/chat", "/api/ollama/generate",
                   "/api/llmprecogn/status", "/api/llmprecogn/providers", "/api/llmprecogn/chat",
                   "/api/llmprecogn/analyse", "/api/understand"]
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


# ═══════════════════════════════════════════════════════════════════════════════
# LLMPreCogn — proxy vers le worker Cloudflare (groq, cerebras, deepseek…)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/llmprecogn/status")
async def llmprecogn_status():
    return connector_llmprecogn.status()

@app.get("/api/llmprecogn/providers")
async def llmprecogn_providers():
    return connector_llmprecogn.providers()

@app.post("/api/llmprecogn/chat")
async def llmprecogn_chat(payload: dict):
    return connector_llmprecogn.chat(payload)

@app.post("/api/llmprecogn/analyse")
async def llmprecogn_analyse(payload: dict):
    return connector_llmprecogn.analyse(payload)


# ═══════════════════════════════════════════════════════════════════════════════
# Understand — point d'entrée de compréhension (briques + LLMPreCogn + Ollama)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/understand")
async def understand(payload: dict):
    """Interprète un message dans le contexte brique d'une org.

    Body :
      - orgId        (str) : obligatoire
      - message      (str) : obligatoire
      - lastMessage  (str) : optionnel, message précédent pour contexte conversationnel
      - documentText (str) : optionnel, texte déjà extrait par Docling
      - documentBase64  (str) : optionnel, document brut en base64 (Analyzor l'extrait via Docling)
      - documentFilename (str) : optionnel, pour détecter le type si base64 fourni

    Retourne {intent, response?, libelle?, montant?, ...} — voir understand.py.
    """
    org_id = payload.get("orgId")
    message = payload.get("message")
    if not org_id or not message:
        return JSONResponse({"success": False, "error": "orgId et message requis"}, status_code=400)

    try:
        result = _understand.understand(
            org_id=org_id,
            message=message,
            last_message=payload.get("lastMessage", ""),
            document_text=payload.get("documentText", ""),
            document_base64=payload.get("documentBase64", ""),
            document_filename=payload.get("documentFilename", ""),
        )
        result["success"] = True
        return result
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


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

        # Enregistrer le document dans Docling (registre central d'information)
        if orgId:
            try:
                docling_registry.record_document(
                    org_id=orgId,
                    filename=file.filename,
                    doc_type="xlsx",
                    extracted_sheets=len(sheets),
                    classification=" | ".join(sorted(set(
                        o["classification"] for o in onglets_rapport
                        if o["classification"] in ("grand_livre_identifie", "blocs_de_compte_groupes")
                    ) or ["non_classe"])),
                    postings_extracted=len(postings),
                    reconciliation_rate=taux_reconciliation,
                )
            except Exception:
                pass

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
SUBSCRIPTIONS_SERVICE_KEY = ''


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


def _account_rows_from_csv(csv_output, compte, begin_date=None):
    """Lignes [date, libellé COMPLET, débit, crédit, solde] d'un onglet de compte, construites
    depuis la sortie `csv` de ledger (libellés entiers) — jamais depuis `register`, dont la
    sortie TEXTE tronque libellé et compte à largeur de colonne fixe en y injectant des '..'
    (root cause des 84 libellés pollués type 'Patrimonia Paiement.. 451001:AMSELLEM' trouvés
    dans le sheet Ponia, 2026-08-14). Le solde cumulé est recalculé ici (le csv ne le donne pas),
    à l'identique du running total de `register --begin` : cumul sur les seules lignes affichées.

    csv colonnes : date, "", libellé, compte(ex '451001:AMSELLEM'), devise, montant, '*', "".
    `compte` : code de l'onglet (ex '451001', '5121', '701'). `begin_date` : 'YYYY/MM/DD' pour
    borner à l'exercice courant (None = tout l'historique, ex. copropriétaires 451xxx)."""
    import csv as _csv, io as _io
    rows = []
    solde = 0.0
    for row in _csv.reader(_io.StringIO(csv_output or '')):
        if len(row) < 6:
            continue
        date_str, _, libelle, compte_full, _devise, amount_raw = row[0], row[1], row[2], row[3], row[4], row[5]
        if compte_full.split(':', 1)[0] != compte:
            continue
        if begin_date and date_str < begin_date:
            continue
        try:
            montant = round(float(amount_raw), 2)
        except ValueError:
            continue
        solde = round(solde + montant, 2)
        debit  = round(montant, 2)  if montant > 0 else ''
        credit = round(-montant, 2) if montant < 0 else ''
        rows.append([date_str, libelle.rstrip('. '), debit, credit, solde])
    return rows


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
    _depenses_tab = None

    # CSV complet du journal, récupéré UNE fois (libellés entiers, jamais tronqués comme
    # `register`) — sert à la fois aux onglets de compte (_account_rows_from_csv) et à l'onglet
    # Journal plus bas. Source unique, pas un appel ledger par onglet.
    try:
        resp_csv = requests.post(
            f'{ledger_url}/api/ledger/query',
            json={'orgId': org_id, 'command': 'csv'},
            timeout=30,
        )
        csv_all = resp_csv.json()
    except Exception as e:
        csv_all = {'success': False, 'error': str(e)}
    csv_output = csv_all.get('output', '') if csv_all.get('success') else ''

    from datetime import date as _date
    current_year = _date.today().year

    for tab in tabs:
        title = tab['title']
        if not title.startswith('C - '):
            continue
        if title in ('C - Balance', 'C - Journal'):
            continue
        if '6 Dépenses' in title or '6 DEPENSES' in title:
            # Traité séparément ci-dessous (blocs par compte 6xx)
            _depenses_tab = title
            continue

        # Extraire le code compte depuis le nom de l'onglet
        m = _re.search(r'\b(\d{3,6})\b', title)
        if not m:
            continue
        compte = m.group(1)

        if not csv_all.get('success'):
            rapport.append({'onglet': title, 'status': 'erreur', 'error': csv_all.get('error')})
            continue

        # Exercice courant : seules les 451xxx (copropriétaires) gardent tout l'historique.
        # Tous les autres comptes : exercice courant uniquement (01/01 de l'année en cours).
        begin_date = None if compte.startswith('451') else f'{current_year}/01/01'

        # Lignes construites depuis le CSV (libellés COMPLETS), jamais depuis register (tronqué)
        rows = _account_rows_from_csv(csv_output, compte, begin_date=begin_date)

        # TOUJOURS vider la plage de données d'abord — même sans ligne à écrire. Sans ça, un
        # compte sans activité sur l'exercice courant gardait indéfiniment son ancienne donnée
        # (souvent tronquée/périmée) au lieu d'un onglet vide (bug trouvé 2026-08-14 : C - 401 et
        # C - 102004 conservaient des libellés pollués car marqués "vide" donc jamais nettoyés).
        try:
            clear_sheet_range(sheet_id, f"'{title}'!A3:E1000")
            if not rows:
                rapport.append({'onglet': title, 'status': 'vide', 'n': 0})
                continue
            range_name = f"'{title}'!A3:E{2 + len(rows)}"
            write_sheet_range(sheet_id, range_name, rows)
            rapport.append({'onglet': title, 'status': 'ok', 'compte': compte, 'n': len(rows)})
        except Exception as e:
            rapport.append({'onglet': title, 'status': 'erreur_ecriture', 'error': str(e)})

    # ── Onglet Journal : toutes les écritures (format csv = noms complets) ──────
    import csv as _csv, io as _io

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

        # Trouver l'onglet Journal (nouveau nom C - Journal ou ancien Journal)
        journal_tab = next((t for t in tabs if t['title'] in ('C - Journal', 'Journal')), None)
        if journal_tab:
            jtitle = journal_tab['title']
            try:
                clear_sheet_range(sheet_id, f"'{jtitle}'!A1:E10000")
                write_sheet_range(sheet_id, f"'{jtitle}'!A1:E{len(journal_rows)}", journal_rows)
                rapport.append({'onglet': jtitle, 'status': 'ok', 'n': len(journal_rows) - 1})
            except Exception as e:
                rapport.append({'onglet': jtitle, 'status': 'erreur_ecriture', 'error': str(e)})
        else:
            rapport.append({'onglet': 'C - Journal', 'status': 'onglet_absent',
                            'note': "Créer un onglet nommé 'C - Journal' dans le sheet pour l'activer"})

    # ── Onglet C - Balance (bilan exercice courant) et A - Exercice YYYY (archives) ──
    _BAL_LINE = _re.compile(r'(-?[\d,]+\.\d{2})\s+EUR\s+(.+)')

    def _parse_balance(output):
        rows = []
        for line in (output or '').splitlines():
            m = _BAL_LINE.search(line)
            if not m:
                continue
            amount_str = m.group(1).replace(',', '')
            full_account = m.group(2).strip()
            code, label = (full_account.split(':', 1) if ':' in full_account
                           else (full_account, full_account))
            try:
                amount = round(float(amount_str), 2)
            except ValueError:
                continue
            rows.append([code.strip(), label.strip(), amount])
        return rows

    from datetime import date as _date
    current_year = _date.today().year

    balance_tabs = []
    for tab in tabs:
        title = tab['title']
        if title == 'C - Balance':
            balance_tabs.append((title, f'{current_year}/01/01', None, f'Solde {current_year} EUR'))
        elif title.startswith('A - Exercice '):
            year_m = _re.search(r'(\d{4})', title)
            if year_m:
                y = year_m.group(1)
                balance_tabs.append((title, f'{y}/01/01', f'{y}/12/31', f'Solde {y} EUR'))

    for title, begin_dt, end_dt, col_label in balance_tabs:
        try:
            qbody = {'orgId': org_id, 'command': 'balance', 'filters': []}
            if begin_dt:
                qbody['beginDate'] = begin_dt
            if end_dt:
                qbody['endDate'] = end_dt
            resp_b = requests.post(f'{ledger_url}/api/ledger/query', json=qbody, timeout=15)
            bal = resp_b.json()
        except Exception as e:
            rapport.append({'onglet': title, 'status': 'erreur', 'error': str(e)})
            continue
        if not bal.get('success'):
            rapport.append({'onglet': title, 'status': 'erreur', 'error': bal.get('error')})
            continue
        year_label = begin_dt[:4] if begin_dt else str(current_year)
        bal_rows = _parse_balance(bal.get('output', ''))
        all_rows = ([[f'Balance Exercice {year_label}', '', ''],
                     ['Compte', 'Libellé', col_label]] + bal_rows)
        try:
            clear_sheet_range(sheet_id, f"'{title}'!A1:C1000")
            write_sheet_range(sheet_id, f"'{title}'!A1:C{len(all_rows)}", all_rows)
            rapport.append({'onglet': title, 'status': 'ok', 'n': len(bal_rows)})
        except Exception as e:
            rapport.append({'onglet': title, 'status': 'erreur_ecriture', 'error': str(e)})

    # ── Onglet C - 6 Dépenses (blocs par compte 6xx, exercice courant) ──────────
    if _depenses_tab:
        try:
            # 1. Balance 6xx pour connaître les comptes actifs cette année
            resp_6 = requests.post(f'{ledger_url}/api/ledger/query', json={
                'orgId': org_id, 'command': 'balance', 'filters': ['6'],
                'beginDate': f'{current_year}/01/01', 'endDate': f'{current_year+1}/01/01',
            }, timeout=15)
            bal_6 = resp_6.json()
            comptes_6 = []
            if bal_6.get('success'):
                for line in bal_6.get('output', '').splitlines():
                    m = _BAL_LINE.search(line)
                    if not m:
                        continue
                    full = m.group(2).strip()
                    code = full.split(':')[0].strip()
                    if not code.startswith('6'):
                        continue
                    label = full.split(':', 1)[1].strip() if ':' in full else full
                    try:
                        amt = round(float(m.group(1).replace(',', '')), 2)
                    except ValueError:
                        continue
                    comptes_6.append((code, label, amt))

            # 2. Construire les blocs
            dep_rows = [[f'Dépenses {current_year}', '', '', '']]
            dep_rows.append(['Compte', 'Date', 'Libellé', 'Montant EUR'])
            total_general = 0.0

            for code, label, solde_annuel in comptes_6:
                # En-tête du bloc
                dep_rows.append([f'{code} — {label}', '', '', ''])
                # Register pour ce compte cette année
                resp_r = requests.post(f'{ledger_url}/api/ledger/query', json={
                    'orgId': org_id, 'command': 'register', 'filters': [code],
                    'beginDate': f'{current_year}/01/01', 'endDate': f'{current_year+1}/01/01',
                }, timeout=15)
                reg = resp_r.json()
                bloc_total = 0.0
                if reg.get('success'):
                    for line in (reg.get('output') or '').splitlines():
                        if not line.strip():
                            continue
                        first_part = _re.split(r'\s{2,}', line.strip())[0]
                        date_m = _DATE_RE.match(first_part)
                        if not date_m:
                            continue
                        try:
                            date_obj = _dt.strptime(date_m.group(1), '%y-%b-%d')
                            date_str = date_obj.strftime('%Y/%m/%d')
                        except ValueError:
                            continue
                        libelle = date_m.group(2).rstrip('. ')
                        amounts = _AMT_RE.findall(line)
                        if len(amounts) < 2:
                            continue
                        montant = round(float(amounts[-2].replace(',', '')), 2)
                        dep_rows.append(['', date_str, libelle, montant])
                        bloc_total += montant
                dep_rows.append(['', '', f'Sous-total {code}', round(bloc_total, 2)])
                dep_rows.append(['', '', '', ''])
                total_general += bloc_total

            dep_rows.append(['', '', 'TOTAL DÉPENSES', round(total_general, 2)])

            clear_sheet_range(sheet_id, f"'{_depenses_tab}'!A1:D5000")
            write_sheet_range(sheet_id, f"'{_depenses_tab}'!A1:D{len(dep_rows)}", dep_rows)
            rapport.append({'onglet': _depenses_tab, 'status': 'ok', 'n': len(comptes_6)})
        except Exception as e:
            rapport.append({'onglet': _depenses_tab, 'status': 'erreur', 'error': str(e)})

    # ── Onglets A - Relevés YYYY (annexes individuelles par copropriétaire) ──────
    import json as _json
    from pathlib import Path as _Path

    rule9_path = _Path('/home/ubuntu/ledger_api/modules/compta_copro/bricks/rule_0009_appel_fonds.json')
    annex_tabs = [t for t in tabs if _re.match(r"A - Relevés \d{4}$", t['title'])]

    if annex_tabs and rule9_path.exists():
        rule9 = _json.loads(rule9_path.read_text())
        copros = rule9.get('copropriétaires', [])
        _AMT2 = _re.compile(r'(-?[\d,]+\.\d{2})\s+EUR')
        _DT2  = _re.compile(r'^(\d{2}-[A-Za-z]{3}-\d{2})\s+(.*)')

        def _parse_register(text, compte):
            """Parse ledger register pour un compte : retourne liste [date, libellé, montant, solde]."""
            rows = []
            for line in text.splitlines():
                m = _DT2.match(line.strip())
                if not m: continue
                raw_date, rest = m.group(1), m.group(2)
                try:
                    d = _datetime.strptime(raw_date, '%y-%b-%d')
                    ds = d.strftime('%Y/%m/%d')
                except: continue
                amounts = _AMT2.findall(line)
                if len(amounts) < 2: continue
                libelle = rest.split('  ')[0].strip()[:50]
                montant = round(float(amounts[-2].replace(',', '')), 2)
                solde   = round(float(amounts[-1].replace(',', '')), 2)
                rows.append([ds, libelle, montant, solde])
            return rows

        from datetime import datetime as _datetime

        for tab in annex_tabs:
            year_m = _re.search(r'(\d{4})', tab['title'])
            if not year_m: continue
            yr = year_m.group(1)
            begin_yr, end_yr = f'{yr}/01/01', f'{yr}/12/31'

            all_rows = [[f'Relevés individuels — Exercice {yr}', '', '', '', ''],
                        ['', '', '', '', '']]
            for copro in copros:
                cpt = copro['compte']
                nom = copro['label']
                # Register filtré par année
                try:
                    reg_resp = requests.post(
                        f'{ledger_url}/api/ledger/query',
                        json={'orgId': org_id, 'command': 'register',
                              'filters': [cpt], 'beginDate': begin_yr, 'endDate': end_yr},
                        timeout=15
                    )
                    reg_text = reg_resp.json().get('output', '')
                except Exception:
                    reg_text = ''

                mvts = _parse_register(reg_text, cpt)
                all_rows.append([f'{nom} ({cpt})', '', '', '', ''])
                all_rows.append(['Date', 'Libellé', 'Débit EUR', 'Crédit EUR', 'Solde EUR'])
                for ds, lib, mnt, sol in mvts:
                    debit  = mnt if mnt > 0 else ''
                    credit = -mnt if mnt < 0 else ''
                    all_rows.append([ds, lib, debit or '', credit or '', sol])
                if not mvts:
                    all_rows.append(['—', 'Aucun mouvement', '', '', ''])
                # Totaux
                tot_deb = sum(m[2] for m in mvts if m[2] > 0)
                tot_cre = sum(-m[2] for m in mvts if m[2] < 0)
                sol_fin = mvts[-1][3] if mvts else 0
                all_rows.append(['', 'TOTAL', tot_deb, tot_cre, sol_fin])
                all_rows.append(['', '', '', '', ''])

            try:
                clear_sheet_range(sheet_id, f"'{tab['title']}'!A1:E2000")
                write_sheet_range(sheet_id, f"'{tab['title']}'!A1:E{len(all_rows)}", all_rows)
                rapport.append({'onglet': tab['title'], 'status': 'ok', 'n': len(copros)})
            except Exception as e:
                rapport.append({'onglet': tab['title'], 'status': 'erreur_ecriture', 'error': str(e)})

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


@app.get("/api/account/module-orgs")
async def account_module_orgs(email: str, module: str):
    """Orgs d'un user filtrées par leur module (source unique de vérité : `contenu.module` sur
    la brique Organisation, dans le Drive de l'org — voir feedback Stéphane 2026-08-13, décision
    d'ancrer le module en BYOS). Utilisé par structory.ai/comptacopro après sign-in Google
    (email extrait du JWT côté client). Ex: ?email=foo@bar.com&module=compta_copro

    `parentOrgId` n'est PLUS le champ de filtrage : c'était une confusion — parentOrgId est la
    hiérarchie parent/fille d'organisations, jamais le module. On garde une lecture de secours
    sur parentOrgId uniquement pour les orgs anciennes pas encore remigrées (aucune régression)."""
    memberships = _bricks.lookup_by_email(email)
    result = []
    for m in memberships:
        org = _bricks.get_org(m['orgId'])
        if not org:
            continue
        contenu = org.get('contenu') or {}
        org_module = contenu.get('module') or contenu.get('parentOrgId', '')  # parentOrgId = repli legacy
        # Cas précis "structorydemo" (retour de Stéphane 2026-08-14 : son org réelle, créée
        # 2026-07-26 avant l'introduction du module 'structory_compta', n'apparaissait plus dans
        # "mes organisations"). Alias ciblé sur cet orgId précis, PAS un alias générique
        # parentOrgId=='structory' -> 'structory_compta' : "suivre_mes_comptes" partage le même
        # parentOrgId legacy ('structory') mais est un module totalement différent (patrimoine,
        # pas comptabilité) — un alias large l'aurait fait apparaître ici à tort (vérifié).
        if m['orgId'] == 'structorydemo' and org_module == 'structory' and module == 'structory_compta':
            org_module = 'structory_compta'
        if org_module == module:
            result.append({
                'orgId': m['orgId'],
                'name': contenu.get('name') or org.get('title') or m['orgId'],
                'role': m['role'],
            })
    return {"success": True, "orgs": result}


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


@app.post("/api/demo/{org_id}/reset")
async def demo_reset(org_id: str):
    """Remet une org de démo à son état pristine.
    Les fichiers Drive sont immuables en démo (écriture service account bloquée),
    donc le reset consiste uniquement à vider les caches mémoire et forcer un rechargement
    depuis Drive — qui contient déjà l'état pristine.
    Seuls les org_id listés dans DEMO_ORGS sont autorisés."""
    import pathlib

    DEMO_ORGS = {'miroadev'}
    if org_id not in DEMO_ORGS:
        return JSONResponse({"success": False, "errorCode": "not_a_demo_org"}, status_code=403)

    tpl_path = pathlib.Path(__file__).parent / 'demo_templates' / org_id / 'pristine.json'
    if not tpl_path.exists():
        return JSONResponse({"success": False, "errorCode": "template_not_found"}, status_code=404)

    tpl = json.loads(tpl_path.read_text())
    folder_id = tpl['folder_id']

    # Vider tous les caches mémoire pour cet org — Drive est déjà l'état pristine
    for bt in ('User', 'Compte', 'Rule', 'Organisation', None):
        _bricks._list_bricks_cache.pop((folder_id, bt), None)
    _bricks._email_index['built_at'] = 0   # force rebuild complet au prochain appel

    # Forcer la relecture immédiate depuis Drive
    users = _bricks.list_users(org_id)
    _bricks._rebuild_email_index()

    return {
        "success": True,
        "org_id": org_id,
        "users_reloaded": len(users),
        "users": [{"uid": u.get("uid"), "name": u.get("title"), "role": (u.get("contenu") or {}).get("role")} for u in users],
    }


@app.post("/api/org/{org_id}/comptes/invalidate-cache")
async def org_invalidate_comptes_cache(org_id: str):
    """À appeler après toute écriture de brique Compte qui contourne ce service (ex.
    identityCreateCompte, Apps Script/DriveApp) — voir bricks.invalidate_comptes_cache."""
    _bricks.invalidate_comptes_cache(org_id)
    return {"success": True}


@app.get("/api/org/{org_id}/folder")
async def org_folder(org_id: str):
    """Résout uniquement le dossier Drive d'une org — contrairement à GET /api/org/{org_id},
    ne nécessite pas qu'une brique Organisation existe (cas de smcspl/smcdemo, créées avant
    l'existence de cette brique). Utilisé par ConnectorIdentity.js::identityCreateCompte, qui a
    besoin du folderId pour écrire directement via DriveApp (contournement du blocage de quota
    du compte de service, voir connector_ownstorage.py)."""
    folder_id = _bricks._folder_id_for_org(org_id)
    if not folder_id:
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    return {"success": True, "folderId": folder_id}


@app.get("/api/org/{org_id}/comptes")
async def org_list_comptes(org_id: str):
    """Liste les comptes patrimoine d'une organisation — utilisé par le Navigator et
    l'Executor (via /api/org/{org_id}/bricks?type=Compte, équivalent générique)."""
    if not _bricks._folder_id_for_org(org_id):
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    return {"success": True, "comptes": _bricks.list_comptes(org_id)}


@app.delete("/api/org/{org_id}/comptes/{compte_uid}")
async def org_delete_compte(org_id: str, compte_uid: str):
    """Met à la corbeille un compte patrimoine (jamais de suppression définitive) — voir
    bricks.delete_compte. Fonctionne même là où la création reste bloquée
    (storageQuotaExceeded), la suppression ne consomme pas de quota Drive."""
    result = _bricks.delete_compte(org_id, compte_uid)
    if result.get('success'):
        return result
    status = 404 if result.get('errorCode') in ('unknown_org', 'compte_introuvable') else 400
    return JSONResponse(result, status_code=status)


@app.get("/api/org/{org_id}/bricks")
async def org_bricks(org_id: str, type: Optional[str] = None):
    """Liste les briques d'un type donné dans le dossier Drive de cette org (générique —
    utilisé notamment par l'Executor pour lister les objets Compte d'une organisation,
    Suivre Mes Comptes ARCHITECTURE.md §1.1)."""
    folder_id = _bricks._folder_id_for_org(org_id)
    if not folder_id:
        return JSONResponse({"success": False, "errorCode": "unknown_org"}, status_code=404)
    return {"success": True, "bricks": _bricks.list_bricks(folder_id, type)}


@app.get("/api/analyzor/rules")
async def analyzor_rules(orgId: str):
    """Rule bricks RÉELLES du module de l'org (ledger_api/modules/{module}/bricks/*.json) —
    jamais exposées en HTTP jusqu'ici, seulement lues côté serveur par understand.py pour le
    chat (2026-08-08). Sert à afficher les vraies règles côté Navigator (permanent, lisible),
    au lieu des Rules de démo codées en dur (getTestPatrimoine côté Navigator). Le vecteur
    _embedding (768 floats) est retiré — inutile et lourd pour un simple affichage."""
    import understand as _und
    module = _und._get_module(orgId)
    bricks = _und._get_bricks_raw(module)
    cleaned = [
        {k: v for k, v in b.items() if not k.startswith('_')}
        for b in bricks
    ]
    return {"success": True, "module": module, "rules": cleaned}


@app.get("/api/ownstorage/journal")
async def ownstorage_journal_get(orgId: str):
    """Contenu actuel du journal ledger-cli depuis le Drive de l'organisation — jamais le
    VPS (2026-08-10, retour de Stéphane : "le journal doit être dans le storage de l'orga,
    pas sur le VPS"). Voir own_storage_journal.py pour le protocole de bootstrap en 2 temps."""
    import own_storage_journal as _oj
    result = _oj.get_journal(orgId)
    status = 200 if result.get('success') else (404 if result.get('errorCode') == 'unknown_org' else 409)
    return JSONResponse(result, status_code=status)


@app.post("/api/ownstorage/journal")
async def ownstorage_journal_set(payload: dict):
    """Remplace le contenu du journal dans le Drive de l'org. Body: {orgId, content}."""
    import own_storage_journal as _oj
    org_id = payload.get('orgId', '')
    content = payload.get('content', '')
    if not org_id:
        return JSONResponse({'success': False, 'error': 'orgId requis'}, status_code=400)
    result = _oj.set_journal(org_id, content)
    status = 200 if result.get('success') else (404 if result.get('errorCode') == 'unknown_org' else 409)
    return JSONResponse(result, status_code=status)


@app.post("/api/ownstorage/releve/append")
async def ownstorage_releve_append(payload: dict):
    """JournaldeBanque (2026-08-14) — ajoute un snapshot sanctuarisé (immuable, jamais réécrit)
    au relevé `name` de l'org. Body: {orgId, name, record}. Voir own_storage_releves.py pour le
    protocole (append-only, repli local pour les orgs sans dossier Drive)."""
    import own_storage_releves as _or
    org_id = payload.get('orgId', '')
    name = payload.get('name', '')
    record = payload.get('record')
    if not org_id or not name or not isinstance(record, dict):
        return JSONResponse({'success': False, 'error': 'orgId, name, record (objet) requis'}, status_code=400)
    result = _or.append_releve(org_id, name, record)
    status = 200 if result.get('success') else 409
    return JSONResponse(result, status_code=status)


@app.get("/api/ownstorage/releve")
async def ownstorage_releve_get(orgId: str, name: str):
    """Relit un relevé sanctuarisé déjà écrit (vérification/debug). Body absent volontairement
    (GET) : jamais utilisé pour reconstruire le journal, uniquement pour prouver/consulter."""
    import own_storage_releves as _or
    records = _or.read_releve(orgId, name)
    return JSONResponse({'success': True, 'records': records, 'count': len(records)})


@app.get("/api/connectors/resolve")
async def connectors_resolve(etablissement: str, nature: str, orgId: Optional[str] = None, module: Optional[str] = None):
    """Résout les connectors compatibles pour un établissement + une nature de compte
    (Suivre Mes Comptes ARCHITECTURE.md §6) — utilisé par l'Executor, jamais par
    l'utilisateur ni le Navigator directement (§2)."""
    matches = resolve_connectors(etablissement, nature, org_id=orgId, module=module)
    return {"success": True, "connectors": matches}


@app.post("/api/connectors/resolve-batch")
async def connectors_resolve_batch(request: Request):
    """Version batch de /api/connectors/resolve (2026-08-02, retour de Stéphane : "le chargement
    patrimoine est toujours trop long... on dirait que c'est planté") — root cause trouvée :
    Executor::_patrimoine_view_data faisait UN appel HTTP séparé PAR COMPTE (jusqu'à 19-20 pour
    smcspl/smcdemo) juste pour connaître le syncMode, alors que resolve_connectors lit les MÊMES
    briques Rule (cache déjà partagé par dossier, `_cached_bricks`) pour tous. Un seul appel
    ici, une seule fois le scan Drive à froid s'il l'est, jamais 19 allers-retours réseau
    séquentiels pour la même donnée sous-jacente.
    Body: {orgId, module?, comptes: [{etablissement, nature}, ...]} — renvoie un tableau dans le
    MÊME ORDRE que `comptes` en entrée (jamais une correspondance par nom, qui casserait sur des
    doublons établissement+nature)."""
    body = await request.json()
    org_id = body.get("orgId")
    module = body.get("module")
    comptes = body.get("comptes") or []
    results = [
        resolve_connectors(c.get("etablissement"), c.get("nature"), org_id=org_id, module=module)
        for c in comptes
    ]
    return {"success": True, "results": results}


@app.get("/api/module/{module}/folder")
async def module_folder(module: str):
    """Dossier Drive d'un module (lecture seule, aucune écriture) — utilisé par
    `identityEnsureConnectorRule` (ConnectorIdentity.js) pour savoir où écrire une nouvelle
    brique Rule connector via DriveApp (le compte de service Analyzor ne peut créer aucun
    nouveau fichier Drive, `storageQuotaExceeded`, voir bricks.create_compte — même
    contournement Apps Script que pour les briques Compte)."""
    folder_id = MODULE_FOLDER_ID.get(module)
    if not folder_id:
        return JSONResponse({"success": False, "errorCode": "unknown_module"}, status_code=404)
    return {"success": True, "folderId": folder_id}


@app.post("/api/connectors/invalidate-cache")
async def connectors_invalidate_cache(request: Request):
    """Invalide le cache de résolution connector d'un module — appelé juste après qu'une
    brique Rule ait été écrite via DriveApp (identityEnsureConnectorRule), sinon invisible
    jusqu'à 6h (TTL de _cached_bricks)."""
    body = await request.json()
    module = body.get("module")
    if not module:
        return JSONResponse({"success": False, "error": "module requis"}, status_code=400)
    invalidate_module_cache(module)
    return {"success": True}


@app.post("/api/copro/appel-fonds")
async def copro_appel_fonds(payload: dict):
    """Émet un appel de fonds trimestriel pour une org copropriété.

    Lit les quotes-parts depuis rule_0009_appel_fonds.json, poste la transaction
    multi-jambes dans le journal (via /api/ledger/import), et retourne un aperçu.

    Body: {orgId, date?: "YYYY/MM/DD", libelle?: str, dryRun?: bool}
    La date et le libellé viennent de rule_0009 si non fournis (prochain Q non comptabilisé).
    """
    org_id = payload.get('orgId', '').strip()
    if not org_id:
        return JSONResponse({'success': False, 'error': 'orgId requis'}, status_code=400)

    # Lire la règle d'appel (rule_0009)
    import json as _json
    from pathlib import Path as _Path
    rule_path = _Path(f'/home/ubuntu/ledger_api/modules/compta_copro/bricks/rule_0009_appel_fonds.json')
    if not rule_path.exists():
        return JSONResponse({'success': False, 'error': 'rule_0009_appel_fonds.json absent'}, status_code=404)
    rule = _json.loads(rule_path.read_text())

    # Déterminer le prochain appel à émettre
    date_appel = payload.get('date')
    libelle = payload.get('libelle')
    if not date_appel or not libelle:
        next_q = next(
            (s for s in rule.get('schedule_2026', []) if s['statut'] == 'à émettre'),
            None
        )
        if not next_q:
            return JSONResponse({'success': False, 'error': 'Aucun appel à émettre dans rule_0009 (tous marqués comptabilisés)'}, status_code=400)
        date_appel = date_appel or next_q['date']
        libelle = libelle or next_q['libelle']

    copros = rule.get('copropriétaires', [])
    compte_prov = rule.get('compte_provision', '701:Prov charges')
    total = round(sum(c['montant_trim'] for c in copros), 2)

    # Construire les jambes
    legs = [
        {'compte': f"{c['compte']}:{c['label']}", 'amount': c['montant_trim']}
        for c in copros
    ]
    legs.append({'compte': compte_prov, 'amount': -total})

    # Vérification d'équilibre
    if abs(sum(l['amount'] for l in legs)) > 0.01:
        return JSONResponse({'success': False, 'error': f'Jambes déséquilibrées (total={sum(l["amount"] for l in legs):.2f})'}, status_code=400)

    dry_run = payload.get('dryRun', False)
    if dry_run:
        return {
            'success': True,
            'dryRun': True,
            'date': date_appel,
            'libelle': libelle,
            'total': total,
            'legs': legs,
        }

    # Poster dans le journal
    try:
        resp = requests.post(
            f'{LEDGER_API_URL}/api/ledger/import',
            json={'orgId': org_id, 'entries': [{'date': date_appel, 'libelle': libelle, 'legs': legs}]},
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)

    if not result.get('success'):
        return JSONResponse({'success': False, 'error': result.get('error')}, status_code=400)

    return {
        'success': True,
        'date': date_appel,
        'libelle': libelle,
        'total': total,
        'legs': legs,
        'email_pending': [
            {'copro': c['label'], 'compte': c['compte'], 'montant': c['montant_trim'], 'email': c.get('email')}
            for c in copros
        ],
        'note': 'Écriture postée. Remplir les champs email dans rule_0009 puis appeler /api/copro/appel-fonds/emails pour envoyer les avis.',
    }


@app.get("/api/copro/appel-fonds/email-preview")
async def copro_appel_fonds_email_preview(orgId: str, trimestre: str = None):
    """Prépare les emails d'appel de fonds sans les envoyer.

    Logique de routage : si mandataire.email existe → destinataire = mandataire,
    sinon → destinataire = email direct du copropriétaire.

    Retourne la liste {to, copro, compte, montant, subject, body} prête à envoyer
    via MailApp (Communicator) ou tout autre transport.

    Query: orgId (requis), trimestre (ex: "Q4" — sinon premier Q 'à émettre')
    """
    from pathlib import Path as _Path
    import json as _json

    rule_path = _Path('/home/ubuntu/ledger_api/modules/compta_copro/bricks/rule_0009_appel_fonds.json')
    if not rule_path.exists():
        return JSONResponse({'success': False, 'error': 'rule_0009 absent'}, status_code=404)
    rule = _json.loads(rule_path.read_text())

    # Trouver le trimestre cible
    schedule = rule.get('schedule_2026', [])
    if trimestre:
        q = next((s for s in schedule if s['trimestre'] == trimestre), None)
    else:
        q = next((s for s in schedule if s['statut'] == 'à émettre'), None)
    if not q:
        return JSONResponse({'success': False, 'error': 'Trimestre introuvable ou tous comptabilisés'}, status_code=400)

    org = _bricks.get_org(orgId)
    iban = (((org or {}).get('contenu') or {}).get('iban')) \
        or rule.get('iban_copropriete') \
        or '(IBAN à définir — tapez "iban FRXX..." dans le Communicator)'
    date_str = q['date'].replace('/', '-')  # YYYY-MM-DD pour l'email
    emails = []
    for c in rule.get('copropriétaires', []):
        mand = c.get('mandataire') or {}
        if mand and mand.get('email'):
            to = mand['email']
            destinataire = f"{mand.get('nom', 'Mandataire')} (pour {c['label']})"
        else:
            to = c.get('email') or ''
            destinataire = c['label']

        if not to:
            emails.append({'copro': c['label'], 'compte': c['compte'], 'montant': c['montant_trim'],
                           'to': None, 'error': 'email manquant'})
            continue

        montant = c['montant_trim']
        subject = f"Appel de fonds {q['trimestre']} 2026 — SDC 45 Boulevard Poniatowski"
        body = (
            f"Madame, Monsieur,\n\n"
            f"Nous vous adressons l'appel de fonds du {q['libelle']} pour la copropriété "
            f"du 45 Boulevard Poniatowski, Paris 12e (SDC).\n\n"
            f"Copropriétaire : {c['label']}\n"
            f"Compte : {c['compte']}\n"
            f"Montant dû : {montant:.2f} EUR\n"
            f"Date d'exigibilité : {date_str}\n\n"
            f"Virement à effectuer :\n"
            f"  IBAN : {iban}\n"
            f"  Référence : APPEL {q['trimestre']}26 {c['compte']}\n\n"
            f"Cordialement,\n"
            f"Le syndic bénévole — SDC 45 Poniatowski"
        )
        emails.append({
            'copro': c['label'],
            'compte': c['compte'],
            'montant': montant,
            'to': to,
            'destinataire': destinataire,
            'subject': subject,
            'body': body,
        })

    total = sum(e['montant'] for e in emails)
    return {
        'success': True,
        'trimestre': q['trimestre'],
        'date': q['date'],
        'total': total,
        'emails': emails,
    }


@app.get("/api/copro/budget")
async def copro_budget(orgId: str):
    """Retourne le budget prévisionnel + dépenses réalisées de l'année courante.

    Lit rule_0009 (budget_annuel_eur, schedule) + interroge le journal ledger
    sur les comptes 6xx pour obtenir les dépenses réalisées.
    """
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt

    rule_path = _Path('/home/ubuntu/ledger_api/modules/compta_copro/bricks/rule_0009_appel_fonds.json')
    if not rule_path.exists():
        return JSONResponse({'success': False, 'error': 'rule_0009 absent'}, status_code=404)
    rule = _json.loads(rule_path.read_text())

    year = _dt.now().year
    budget = rule.get('budget_annuel_eur', 0)
    trimestres = len([s for s in rule.get('schedule_2026', []) if s['statut'] == 'comptabilisé'])
    encaisse = trimestres * rule.get('montant_trimestriel_eur', 0)

    # Dépenses réelles (comptes 6xx) via ledger_api
    try:
        resp = requests.post(
            f'{LEDGER_API_URL}/api/ledger/query',
            json={'orgId': orgId, 'command': 'balance', 'filters': ['6'],
                  'beginDate': f'{year}/01/01', 'endDate': f'{year+1}/01/01'},
            timeout=15,
        )
        output = resp.json().get('output', '')
        # Parser le total final (dernière ligne avec EUR et pas de compte)
        depenses = 0.0
        for line in output.splitlines():
            m = __import__('re').search(r'(-?[\d,]+\.\d{2})\s+EUR\s*$', line)
            if m:
                try:
                    depenses = abs(float(m.group(1).replace(',', '')))
                except Exception:
                    pass
    except Exception:
        depenses = None

    return {
        'success': True,
        'year': year,
        'budget_annuel': budget,
        'montant_trimestriel': rule.get('montant_trimestriel_eur', 0),
        'trimestres_comptabilises': trimestres,
        'encaisse_prevu': encaisse,
        'depenses_realisees': depenses,
        'reste': round(budget - (depenses or 0), 2),
        'taux': round((depenses or 0) / budget * 100, 1) if budget else 0,
    }


@app.post("/api/analyzor/understand/file")
async def analyzor_understand_file(
    orgId: str = Form(...),
    message: str = Form(''),
    lastMessage: str = Form(''),
    file: UploadFile = File(...),
):
    """Variante multipart : reçoit un fichier, l'extrait via Docling, puis understand().

    Formats supportés : PDF, DOCX, XLSX, XLS, CSV.
    Body (multipart/form-data): orgId, message?, lastMessage?, file
    """
    import understand as _und
    import tempfile, os

    if not orgId.strip():
        return JSONResponse({'success': False, 'error': 'orgId requis'}, status_code=400)

    suffix = os.path.splitext(file.filename or 'doc')[1].lower() or '.bin'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        if DOCLING_AVAILABLE:
            doc_text = connector_docling.extract_text(tmp_path)
        else:
            # Fallback texte brut pour CSV / fichiers texte
            try:
                with open(tmp_path, encoding='utf-8', errors='replace') as f:
                    doc_text = f.read(8000)
            except Exception:
                doc_text = ''
    finally:
        os.unlink(tmp_path)

    user_msg = message.strip() or f'Analyse ce document : {file.filename}'
    try:
        result = _und.understand(orgId.strip(), user_msg, lastMessage, doc_text)
        return {'success': True, 'filename': file.filename, **result}
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


@app.post("/api/analyzor/understand")
async def analyzor_understand(payload: dict):
    """Interprète un message utilisateur dans le contexte brique d'une org PreCogn.

    Chaîne : bricks org → LLMPrecogn → Ollama (fallback) → exécution query si besoin.
    Si documentText est fourni (texte pré-extrait par Docling), il est injecté dans le contexte.

    Body: {orgId, message, lastMessage?: str, documentText?: str}
    Retourne: {success, intent, response?, libelle?, montant?, sens?, ...}
    """
    import understand as _und
    org_id   = payload.get('orgId', '').strip()
    message  = payload.get('message', '').strip()
    if not org_id or not message:
        return JSONResponse({'success': False, 'error': 'orgId et message requis'}, status_code=400)
    last_msg   = payload.get('lastMessage', '')
    doc_text   = payload.get('documentText', '')
    doc_b64    = payload.get('documentBase64', '')
    doc_fname  = payload.get('documentFilename', '')
    try:
        result = _und.understand(org_id, message, last_msg, doc_text, doc_b64, doc_fname)
        return {'success': True, **result}
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


@app.post("/api/analyzor/embed-bricks")
async def analyzor_embed_bricks(payload: dict):
    """Génère ou met à jour les embeddings (_embedding) de toutes les briques JSON.

    Body: {module?: str}   — si absent, traite tous les modules.
    Retourne: {success, results: {filename: statut}}
    """
    import embed_bricks as _emb
    module = payload.get('module') or None
    try:
        results = _emb.embed_all_bricks(module=module)
        return {'success': True, 'results': results}
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


@app.post("/api/precogn/facilitateur")
async def precogn_facilitateur(payload: dict):
    """Génère / rafraîchit le sheet facilitateur PreCogn pour une organisation.

    Body: {orgId, sheetId?: str, rebuild?: bool}
    - Premier appel : fournir sheetId (créé manuellement, SA partagé en éditeur).
    - Appels suivants : sheetId optionnel (mémorisé dans data/{orgId}_facilitateur.json).
    """
    import precogn_facilitateur as _fac
    org_id = payload.get('orgId', '').strip()
    if not org_id:
        return JSONResponse({'success': False, 'error': 'orgId requis'}, status_code=400)
    sheet_id = payload.get('sheetId')
    rebuild  = payload.get('rebuild', False)
    try:
        result = _fac.run(org_id, sheet_id=sheet_id, rebuild=rebuild)
        # Marquer le facilitateur comme généré dans Docling (ne sera plus suggéré)
        try:
            docling_registry.mark_facilitateur_generated(org_id, sheet_id=result.get('sheetId'))
        except Exception:
            pass
        return {'success': True, **result}
    except ValueError as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════════
# Docling — registre central (stats, historique, facilitateur)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/docling/stats")
async def docling_stats(orgId: str = None):
    """Statistiques du registre Docling — globales ou filtrées par orgId."""
    return {"success": True, "stats": docling_registry.get_stats(orgId)}


@app.get("/api/docling/orgs")
async def docling_orgs():
    """Liste toutes les organisations enregistrées dans Docling."""
    return {"success": True, "orgs": docling_registry.list_orgs()}


@app.get("/archi", response_class=HTMLResponse)
async def archi():
    """Diagramme d'architecture PreCogn/VPS généré en temps réel."""
    from archi_template import generate_archi_html
    return HTMLResponse(generate_archi_html())


@app.api_route("/api/jdb/{path:path}", methods=["GET", "POST"])
async def _proxy_jdb(path: str, request: Request):
    """Relais vers jdb_api (port 8086) : le worker Cloudflare proxifie tout /api/* vers Analyzor,
    donc /api/jdb/* doit etre reexpedie ici vers le service JournalDeBanque."""
    url = "http://localhost:8086/api/jdb/" + path
    params = dict(request.query_params)
    body = await request.body()
    fwd_headers = {"Content-Type": "application/json"}
    if request.headers.get("X-Service-Key"):
        fwd_headers["X-Service-Key"] = request.headers["X-Service-Key"]
    try:
        if request.method == "GET":
            r = requests.get(url, params=params, headers=fwd_headers, timeout=60)
        else:
            r = requests.post(url, params=params, data=body, headers=fwd_headers, timeout=60)
    except requests.RequestException as e:
        return JSONResponse({"success": False, "error": f"jdb_api injoignable : {e}"}, status_code=502)
    try:
        return JSONResponse(r.json(), status_code=r.status_code)
    except ValueError:
        return JSONResponse({"success": False, "error": "reponse non-JSON de jdb_api"}, status_code=502)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
