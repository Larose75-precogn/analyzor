"""
Docling — registre (et bus d'adressage) de l'écosystème PreCogn : le point central qui sait,
pour un `org_id` donné, où vivent ses données réelles (uid + adresse BYOS — dossier Drive
aujourd'hui, potentiellement un autre backend plus tard, voir connector_ownstorage.py).

C'est un connector au même titre que les autres (`connector_ownstorage.py`,
`connector_docling.py` pour l'extraction XLSX) : Communicator/Navigator appellent Analyzor via
ConnectorDocling.js (bibliotheque), qui parle à ce module. Aucun autre composant ne doit garder
sa propre logique de résolution org_id → adresse — c'est le rôle de Docling.

En fichier local sur le disque d'Analyzor, PAS sur Drive.

Registre étendu (2026-08-03) : en plus de l'adresse BYOS, suit par organisation :
- L'historique des documents analysés (sheettojournal, extraction)
- La dernière analyse understand (intent, message, timestamp)
- Le flag facilitateur (suggéré, généré) — pour proposer sans imposer
- Le(s) embedding(s) connus de l'org (quand la brique org elle-même a été embeddée)
- Statistiques globales (n_orgs, n_analyses, n_documents)
"""

import json
import os
import time

_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), 'data', '_org_registry.json')
_ANALYSES_PATH = os.path.join(os.path.dirname(__file__), 'data', '_docling_analyses.json')
_cache = None
_analyses_cache = None
_MAX_ANALYSES_PER_ORG = 50  # garde les N dernières analyses par org


def _load():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_REGISTRY_PATH) as fh:
            _cache = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _save():
    os.makedirs(os.path.dirname(_REGISTRY_PATH), exist_ok=True)
    with open(_REGISTRY_PATH, 'w') as fh:
        json.dump(_cache, fh, ensure_ascii=False, indent=2)


def _load_analyses():
    global _analyses_cache
    if _analyses_cache is not None:
        return _analyses_cache
    try:
        with open(_ANALYSES_PATH) as fh:
            _analyses_cache = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _analyses_cache = {"_stats": {"n_orgs": 0, "n_analyses": 0, "n_documents": 0}, "orgs": {}}
    return _analyses_cache


def _save_analyses():
    os.makedirs(os.path.dirname(_ANALYSES_PATH), exist_ok=True)
    with open(_ANALYSES_PATH, 'w') as fh:
        json.dump(_analyses_cache, fh, ensure_ascii=False, indent=2)


def _org_slot(org_id):
    analyses = _load_analyses()
    if org_id not in analyses["orgs"]:
        analyses["orgs"][org_id] = {
            "documents": [],
            "analyses": [],
            "facilitateur": {"suggested": False, "generated": False, "sheetId": None},
        }
        analyses["_stats"]["n_orgs"] = len(analyses["orgs"])
    return analyses["orgs"][org_id]


# ── Adressage BYOS (existant, inchangé) ────────────────────────────────────────

def register_address(org_id, uid, folder_id, backend='gdrive'):
    registry = _load()
    registry[org_id] = {'uid': uid, 'backend': backend, 'folderId': folder_id}
    _save()


def resolve_address(org_id):
    return _load().get(org_id)


def list_orgs():
    return _load()


# ── Historique documentaire ─────────────────────────────────────────────────────

def record_document(org_id, filename, doc_type, extracted_sheets=0,
                    classification=None, postings_extracted=0,
                    reconciliation_rate=None):
    """Enregistre un document analysé (sheettojournal, extraction Docling...) dans
    l'historique de l'organisation. Appelé après chaque analyse documentaire réussie."""
    slot = _org_slot(org_id)
    entry = {
        "filename": filename,
        "type": doc_type,
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "extracted_sheets": extracted_sheets,
        "classification": classification,
        "postings_extracted": postings_extracted,
        "reconciliation_rate": reconciliation_rate,
    }
    slot["documents"].append(entry)
    if len(slot["documents"]) > _MAX_ANALYSES_PER_ORG:
        slot["documents"] = slot["documents"][-_MAX_ANALYSES_PER_ORG:]

    analyses = _load_analyses()
    analyses["_stats"]["n_documents"] += 1
    _save_analyses()


def record_understand(org_id, message, intent, response_preview="",
                      provider=None, has_document=False, embedding_used=False):
    """Enregistre une analyse understand dans l'historique de l'organisation."""
    slot = _org_slot(org_id)
    entry = {
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "message": message[:200],
        "intent": intent,
        "response": response_preview[:200],
        "provider": provider,
        "has_document": has_document,
        "embedding_used": embedding_used,
    }
    slot["analyses"].append(entry)
    if len(slot["analyses"]) > _MAX_ANALYSES_PER_ORG:
        slot["analyses"] = slot["analyses"][-_MAX_ANALYSES_PER_ORG:]

    analyses = _load_analyses()
    analyses["_stats"]["n_analyses"] += 1
    _save_analyses()


def get_stats(org_id=None):
    """Statistiques globales ou pour une org précise."""
    analyses = _load_analyses()
    if org_id and org_id in analyses.get("orgs", {}):
        slot = analyses["orgs"][org_id]
        return {
            "n_documents": len(slot["documents"]),
            "n_analyses": len(slot["analyses"]),
            "facilitateur": slot["facilitateur"],
            "last_document": slot["documents"][-1] if slot["documents"] else None,
            "last_analysis": slot["analyses"][-1] if slot["analyses"] else None,
        }
    return analyses.get("_stats", {})


# ── Facilitateur ────────────────────────────────────────────────────────────────

def suggest_facilitateur(org_id):
    """Marque le facilitateur comme 'suggéré' pour cette org. L'API
    /api/understand inclura alors un flag suggestedAction pour que
    l'appelant (Communicator/Navigator) puisse le proposer au user."""
    slot = _org_slot(org_id)
    if not slot["facilitateur"]["generated"]:
        slot["facilitateur"]["suggested"] = True
        _save_analyses()


def mark_facilitateur_generated(org_id, sheet_id=None):
    """Marque le facilitateur comme généré (appelé après POST /api/precogn/facilitateur)."""
    slot = _org_slot(org_id)
    slot["facilitateur"]["generated"] = True
    slot["facilitateur"]["suggested"] = False
    if sheet_id:
        slot["facilitateur"]["sheetId"] = sheet_id
    _save_analyses()


def facilitateur_info(org_id):
    """Retourne l'état du facilitateur pour cette org {suggested, generated, sheetId}."""
    return _org_slot(org_id).get("facilitateur", {})


# ── Invalidation ────────────────────────────────────────────────────────────────

def invalidate():
    global _cache, _analyses_cache
    _cache = None
    _analyses_cache = None
