from fastapi import FastAPI, UploadFile, File, Form
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
from config_resolver import resolve_query_keywords, resolve_table_config

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
                    bloc_postings, incertains = extract_grouped_expense_postings(sheet.tables)
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
