"""
OwnStorage — relevés bancaires sanctuarisés (JournaldeBanque, 2026-08-14).

Spec (§2bis) : puisque le journal est construit à partir d'un relevé bancaire, ce relevé
SOURCE doit lui-même être conservé dans l'Own Storage de l'organisation, intact, intangible,
infalsifiable, non supprimable et non modifiable — le journal reste vérifiable contre sa
preuve d'origine, à tout moment.

Contrainte réelle identique à org_secrets.py/own_storage_journal.py : le compte de service ne
peut JAMAIS créer un nouveau fichier Drive (storageQuotaExceeded), seulement METTRE À JOUR un
fichier existant. Contrairement à ces deux modules (un seul fichier, remplacé en entier à
chaque écriture), un relevé sanctuarisé ne doit JAMAIS être réécrit — seulement complété.
Adaptation retenue (justifiée, pas la lettre du plan qui suggérait un fichier PAR appel — un
nouveau fichier par appel exigerait un aller-retour Apps Script/identité réelle À CHAQUE
fetch_transactions(), impossible pour une synchro automatique/planifiée sans session
utilisateur active) : UN SEUL fichier `.jsonl` par (org, connector, compte), créé UNE FOIS
(placeholder Apps Script, comme pour journal.ledger/secrets), puis complété par append -
chaque ligne déjà écrite n'est jamais retouchée par un appel suivant (vérifiable : relire le
fichier avant et après un 2e appel, les lignes précédentes doivent rester bit-à-bit
identiques). Donne la même garantie d'intangibilité que "un fichier par snapshot", avec une
seule opération de bootstrap au lieu d'une par synchro.

Repli local pur pour les orgs sans dossier Drive (ex. démos internes) — même convention que
`ledger_api::org_journal_path`.
"""

import json
import os
import re
import time

import connector_ownstorage as _drive
import bricks as _bricks

RELEVES_SUBFOLDER_NAME = '_releves'
LOCAL_FALLBACK_DIR = os.path.join(os.path.dirname(__file__), 'data', 'releves_local')

_releves_folder_cache = {}


def _find_file(folder_id, name):
    for f in _drive.list_files(folder_id):
        if f['name'] == name:
            return f['id']
    return None


def _ensure_releves_subfolder(org_folder_id):
    """Le sous-dossier `_releves/` lui-même PEUT être créé par le compte de service
    (create_folder n'est jamais bloqué par storageQuotaExceeded, seule la création de FICHIER
    l'est — vérifié empiriquement, voir connector_ownstorage.py docstring). Seul le premier
    FICHIER `.jsonl` à l'intérieur nécessite le contournement Apps Script."""
    cache_key = org_folder_id
    if cache_key in _releves_folder_cache:
        return _releves_folder_cache[cache_key]

    for f in _drive.list_files(org_folder_id):
        if f['mime_type'] == 'application/vnd.google-apps.folder' and f['name'] == RELEVES_SUBFOLDER_NAME:
            _releves_folder_cache[cache_key] = f['id']
            return f['id']

    folder_id = _drive.create_folder(org_folder_id, RELEVES_SUBFOLDER_NAME)
    _releves_folder_cache[cache_key] = folder_id
    return folder_id


def _local_path(org_id, name):
    safe_org = re.sub(r'[^a-zA-Z0-9_-]', '_', org_id)
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', name)
    org_dir = os.path.join(LOCAL_FALLBACK_DIR, safe_org)
    os.makedirs(org_dir, exist_ok=True)
    return os.path.join(org_dir, safe_name)


def append_releve(org_id, name, record):
    """Ajoute UNE ligne JSON (un snapshot sanctuarisé) au relevé `name` (ex.
    "powens_bcp_stephane_courant.jsonl") de l'org. Ne modifie/supprime jamais une ligne déjà
    écrite — seul un append est possible par construction (lecture intégrale + écriture
    intégrale via update_file, mais le code n'a physiquement aucun chemin qui retire du
    contenu existant).

    Retourne :
    - {'success': True, 'fileId'|'localPath': ..., 'lineCount': int}
    - {'success': False, 'errorCode': 'needs_bootstrap', 'folderId': str, 'filename': str}
      (org avec dossier Drive mais placeholder pas encore créé — voir
      ConnectorIdentity.js::identityEnsureRelevePlaceholder, jamais fait automatiquement ici)
    """
    record = dict(record)
    record.setdefault('sanctuarise_at', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)

    org_folder_id = _bricks._folder_id_for_org(org_id)

    if not org_folder_id:
        # Pas de dossier Drive pour cette org (ex. démo interne) : repli local pur, jamais
        # bloquant — mêmes garanties d'append-only, juste pas sur Drive.
        path = _local_path(org_id, name)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        with open(path, encoding='utf-8') as f:
            line_count = sum(1 for _ in f)
        return {'success': True, 'localPath': path, 'lineCount': line_count}

    releves_folder_id = _ensure_releves_subfolder(org_folder_id)
    file_id = _find_file(releves_folder_id, name)
    if not file_id:
        return {'success': False, 'errorCode': 'needs_bootstrap', 'folderId': releves_folder_id, 'filename': name}

    existing = _drive.read_file(file_id) or ''
    new_content = existing + line + '\n'
    _drive.update_file(file_id, new_content, mime_type='text/plain')
    return {'success': True, 'fileId': file_id, 'lineCount': existing.count('\n') + 1}


def read_releve(org_id, name):
    """Relit un relevé sanctuarisé (vérification/debug — jamais utilisé pour reconstruire le
    journal, qui reste le rôle de jdb_api/ledger_api). Retourne la liste des enregistrements
    JSON déjà écrits, ou [] si le relevé n'existe pas encore."""
    org_folder_id = _bricks._folder_id_for_org(org_id)

    if not org_folder_id:
        path = _local_path(org_id, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    else:
        releves_folder_id = _ensure_releves_subfolder(org_folder_id)
        file_id = _find_file(releves_folder_id, name)
        if not file_id:
            return []
        content = _drive.read_file(file_id) or ''
        lines = content.splitlines()

    records = []
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            records.append(json.loads(raw_line))
        except json.JSONDecodeError:
            continue
    return records
