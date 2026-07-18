"""
Résolution de la table de configuration (entonnoir) d'analyzor, en cascade :

    organisation -> module -> Structory -> PreCogn

Chaque niveau est une ou plusieurs briques Rule (JSON) dans un dossier Drive.
On fusionne du plus général (PreCogn) au plus spécifique (organisation) — un
niveau plus spécifique surcharge les clés qu'il définit (héritage, pas
remplacement total).

BYOS réel : lecture via connector_ownstorage (Google Drive), pas de fichiers
locaux. Mêmes identifiants de dossier que Bibliotheque/ledger_api pour rester
cohérent entre les outils Apps Script et Python.
"""

import time
from connector_ownstorage import list_files, read_file
import json

STRUCTORY_FOLDER_ID = '1vYWtlIxTzZBB4e29J8ymZSdZQxyVkzqz'  # Structory/
COMPTA_COPRO_FOLDER_ID = '1ll52W0IaTt9ZBbKd6VQ0334oj7-toxVA'  # Structory/compta copro/

MODULE_FOLDER_ID = {
    'compta_copro': COMPTA_COPRO_FOLDER_ID,
}

# BYOS v0 : même dossier que le module tant qu'aucun dossier dédié par
# organisation n'existe (cohérent avec Bibliotheque/ContextPreCogn.js).
ORG_FOLDER_ID = {
    'copro_1crE1G2RerFeXQfHNh0yERfvfAjVKGUz53LE9szCqMMs': COMPTA_COPRO_FOLDER_ID,
}

_cache = {}
_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h : la config change rarement


def _read_bricks(folder_id):
    """Lit toutes les briques JSON d'un dossier Drive. Renvoie [] si le
    dossier n'existe pas ou est vide - jamais d'erreur bloquante."""
    if not folder_id:
        return []
    bricks = []
    for f in list_files(folder_id):
        if f['mime_type'] != 'application/json':
            continue
        try:
            bricks.append(json.loads(read_file(f['id'])))
        except (json.JSONDecodeError, Exception):
            continue  # une brique corrompue ne doit pas bloquer les autres
    return bricks


def _merge_tables(bricks, config):
    for brick in bricks:
        table = brick.get('contenu', {}).get('table')
        if table:
            config.update(table)


def resolve_table_config(org_id=None, module=None):
    cache_key = f"{module}:{org_id}"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _CACHE_TTL_SECONDS:
        return cached['config']

    config = {}
    sources = []

    structory_bricks = _read_bricks(STRUCTORY_FOLDER_ID)
    if structory_bricks:
        _merge_tables(structory_bricks, config)
        sources.append('structory')

    if module:
        module_bricks = _read_bricks(MODULE_FOLDER_ID.get(module))
        if module_bricks:
            _merge_tables(module_bricks, config)
            sources.append(f'module:{module}')

    if org_id:
        org_bricks = _read_bricks(ORG_FOLDER_ID.get(org_id))
        if org_bricks:
            _merge_tables(org_bricks, config)
            sources.append(f'org:{org_id}')

    config['_sources'] = sources
    _cache[cache_key] = {'config': config, 't': time.time()}
    return config


def resolve_query_keywords():
    """Vocabulaire de reconnaissance des consultations (garde-fou déterministe
    côté Communicator) — lu depuis les briques Rule du niveau Structory."""
    cache_key = 'query_keywords'
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _CACHE_TTL_SECONDS:
        return cached['config']

    keywords = set()
    for brick in _read_bricks(STRUCTORY_FOLDER_ID):
        for group in brick.get('contenu', {}).values():
            if isinstance(group, list):
                # Seules les listes de chaînes sont du vocabulaire (ex: rule_0002) —
                # d'autres bricks (ex: rule_0001) ont des listes de dicts (plan
                # comptable) qui ne doivent pas se retrouver mélangées ici.
                keywords.update(k.lower() for k in group if isinstance(k, str))

    result = sorted(keywords)
    _cache[cache_key] = {'config': result, 't': time.time()}
    return result
